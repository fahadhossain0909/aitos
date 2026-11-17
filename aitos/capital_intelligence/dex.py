"""Read-only Uniswap V2/V3 pool swap log observer for configured pools."""
from __future__ import annotations
import hashlib
from datetime import datetime,timezone
from typing import Any
import aiohttp
from .contracts import CapitalEvent

V2_SWAP_TOPIC="0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP_TOPIC="0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

class EvmDexSwapAdapter:
    """Decodes swap quantities without inventing USD value or token decimals."""
    def __init__(self,*,rpc_url:str,chain_id:str,pools:dict[str,str],confirmations:int=12,max_block_range:int=250,session:aiohttp.ClientSession|None=None):
        if not rpc_url.startswith("https://") or not pools or len(pools)>300: raise ValueError("HTTPS RPC and 1-300 pools required")
        if not 1<=confirmations<=10000 or not 1<=max_block_range<=2000: raise ValueError("invalid block bounds")
        self.rpc_url=rpc_url; self.chain_id=chain_id; self.pools={self._address(k):v.lower() for k,v in pools.items()}
        if any(v not in {"uniswap_v2","uniswap_v3","pancakeswap_v2","pancakeswap_v3"} for v in self.pools.values()): raise ValueError("unsupported pool protocol")
        self.confirmations=confirmations; self.max_block_range=max_block_range; self.session=session; self.owns_session=session is None; self.request_id=0
    @staticmethod
    def _address(value:str)->str:
        value=value.lower()
        if len(value)!=42 or not value.startswith("0x") or any(c not in "0123456789abcdef" for c in value[2:]): raise ValueError("invalid EVM address")
        return value
    async def close(self):
        if self.owns_session and self.session and not self.session.closed: await self.session.close()
    async def _rpc(self,method:str,params:list[Any]):
        if self.session is None: self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self.request_id+=1
        async with self.session.post(self.rpc_url,json={"jsonrpc":"2.0","id":self.request_id,"method":method,"params":params}) as response:
            response.raise_for_status(); result=await response.json()
        if result.get("error"): raise RuntimeError(f"EVM RPC {method} failed")
        return result.get("result")
    async def poll(self,*,cursor:str|None=None):
        latest=int(await self._rpc("eth_blockNumber",[]),16); safe=latest-self.confirmations
        start=int(cursor,16)+1 if cursor else max(0,safe-self.max_block_range+1)
        if safe<start: return [],cursor
        end=min(safe,start+self.max_block_range-1)
        logs=await self._rpc("eth_getLogs",[{"fromBlock":hex(start),"toBlock":hex(end),"address":list(self.pools),"topics":[[V2_SWAP_TOPIC,V3_SWAP_TOPIC]]}])
        events=[]; now=datetime.now(timezone.utc)
        for log in logs or []:
            topic=log.get("topics",[""])[0].lower(); data=log.get("data","0x")[2:]
            if len(data)<256: continue
            words=[data[i:i+64] for i in range(0,len(data),64)]
            signed=lambda w: int(w,16)-(1<<256) if int(w,16)>=(1<<255) else int(w,16)
            if topic==V2_SWAP_TOPIC:
                if len(words)<4: continue
                amount0_in,amount1_in,amount0_out,amount1_out=[int(w,16) for w in words[:4]]
                delta0=amount0_in-amount0_out; delta1=amount1_in-amount1_out
            elif topic==V3_SWAP_TOPIC:
                if len(words)<2: continue
                delta0,delta1=signed(words[0]),signed(words[1])
            else: continue
            block=int(log["blockNumber"],16); tx=log["transactionHash"]; index=int(log["logIndex"],16)
            pool=self._address(log["address"]); event_id=hashlib.sha256(f"{self.chain_id}|{tx}|{index}".encode()).hexdigest()
            actor="0x"+log["topics"][1][-40:] if len(log.get("topics",[]))>1 else None
            events.append(CapitalEvent(event_id,f"evm:{self.chain_id}","dex_swap","crypto_spot",self.pools[pool],pool,"pool_swap",now,now,abs(delta0)+abs(delta1),"token_base_units_sum",None,actor_id=actor,transaction_id=tx,block_number=block,block_hash=log.get("blockHash"),finality="confirmed",metadata={"amount0_delta":str(delta0),"amount1_delta":str(delta1),"log_index":index,"token0_and_token1_decimals_required":True,"pool":pool}))
        return events,hex(end)
