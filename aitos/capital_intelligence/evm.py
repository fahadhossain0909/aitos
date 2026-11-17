"""Bounded EVM ERC-20 Transfer observer over public JSON-RPC."""
from __future__ import annotations
import hashlib
from datetime import datetime,timezone
from typing import Any
import aiohttp
from .contracts import CapitalEvent

TRANSFER_TOPIC="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

class EVMLogCapitalAdapter:
    def __init__(self,*,rpc_url:str,chain_id:str,contracts:list[str],confirmations:int=12,max_block_range:int=500,timeout_seconds:float=10.0,session:aiohttp.ClientSession|None=None):
        if not rpc_url.startswith("https://"): raise ValueError("RPC URL must use HTTPS")
        if not chain_id.strip() or not contracts or len(contracts)>500: raise ValueError("chain and 1-500 contracts required")
        if not 1<=confirmations<=10000 or not 1<=max_block_range<=5000: raise ValueError("invalid block bounds")
        self.rpc_url=rpc_url; self.chain_id=chain_id; self.contracts=[self._address(a) for a in contracts]
        self.confirmations=confirmations; self.max_block_range=max_block_range; self.timeout_seconds=timeout_seconds
        self.session=session; self.owns_session=session is None; self.request_id=0
    @staticmethod
    def _address(value:str)->str:
        value=value.lower()
        if len(value)!=42 or not value.startswith("0x") or any(c not in "0123456789abcdef" for c in value[2:]): raise ValueError("invalid EVM address")
        return value
    async def close(self):
        if self.owns_session and self.session and not self.session.closed: await self.session.close()
    async def _rpc(self,method:str,params:list[Any])->Any:
        if self.session is None: self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_seconds))
        self.request_id+=1
        async with self.session.post(self.rpc_url,json={"jsonrpc":"2.0","id":self.request_id,"method":method,"params":params}) as response:
            response.raise_for_status(); data=await response.json()
        if data.get("error"): raise RuntimeError(f"EVM RPC {method} failed")
        return data.get("result")
    async def poll(self,*,cursor:str|None=None)->tuple[list[CapitalEvent],str|None]:
        latest=int(await self._rpc("eth_blockNumber",[]),16); end=latest-self.confirmations
        start=int(cursor,16)+1 if cursor else max(0,end-self.max_block_range+1)
        if end<start: return [],cursor
        end=min(end,start+self.max_block_range-1)
        logs=await self._rpc("eth_getLogs",[{"fromBlock":hex(start),"toBlock":hex(end),"address":self.contracts,"topics":[TRANSFER_TOPIC]}])
        now=datetime.now(timezone.utc); events=[]
        for log in logs or []:
            topics=log.get("topics",[])
            if len(topics)<3: continue
            raw_amount=int(log.get("data","0x0"),16); block=int(log["blockNumber"],16); tx=log["transactionHash"]; index=int(log["logIndex"],16)
            source="0x"+topics[1][-40:]; destination="0x"+topics[2][-40:]
            event_id=hashlib.sha256(f"{self.chain_id}|{tx}|{index}".encode()).hexdigest()
            events.append(CapitalEvent(event_id,f"evm:{self.chain_id}","onchain_transfer","crypto_spot",self.chain_id,log["address"].lower(),"erc20_transfer",now,now,float(raw_amount),"token_base_units",None,source,destination,tx,block,log.get("blockHash"),"confirmed",1.0,{"log_index":index,"decimals_required":True}))
        return events,hex(end)
