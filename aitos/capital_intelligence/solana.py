"""Read-only Solana finalized address-activity observer."""
from __future__ import annotations
import hashlib
from datetime import datetime,timezone
from typing import Any
import aiohttp
from .contracts import CapitalEvent

class SolanaAddressActivityAdapter:
    def __init__(self,*,rpc_url:str,addresses:list[str],timeout_seconds:float=10.0,session:aiohttp.ClientSession|None=None):
        if not rpc_url.startswith("https://") or not addresses or len(addresses)>500: raise ValueError("HTTPS RPC and 1-500 addresses required")
        self.rpc_url=rpc_url; self.addresses=sorted(set(addresses)); self.timeout_seconds=timeout_seconds; self.session=session; self.owns_session=session is None; self.request_id=0
    async def close(self):
        if self.owns_session and self.session and not self.session.closed: await self.session.close()
    async def _rpc(self,method:str,params:list[Any])->Any:
        if self.session is None: self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_seconds))
        self.request_id+=1
        async with self.session.post(self.rpc_url,json={"jsonrpc":"2.0","id":self.request_id,"method":method,"params":params}) as response:
            response.raise_for_status(); payload=await response.json()
        if payload.get("error"): raise RuntimeError(f"Solana RPC {method} failed")
        return payload.get("result")
    async def poll(self,*,cursor:str|None=None)->tuple[list[CapitalEvent],str|None]:
        events=[]; newest=cursor; now=datetime.now(timezone.utc)
        for address in self.addresses:
            rows=await self._rpc("getSignaturesForAddress",[address,{"limit":100,"commitment":"finalized"}])
            for item in rows or []:
                signature=item.get("signature")
                if not signature or signature==cursor: continue
                tx=await self._rpc("getTransaction",[signature,{"encoding":"jsonParsed","commitment":"finalized","maxSupportedTransactionVersion":0}])
                if not tx: continue
                meta=tx.get("meta") or {}; slot=tx.get("slot"); stamp=datetime.fromtimestamp(tx.get("blockTime") or now.timestamp(),timezone.utc)
                event_id=hashlib.sha256(f"solana|{address}|{signature}".encode()).hexdigest()
                events.append(CapitalEvent(event_id,"solana-rpc","onchain_transaction","crypto_spot","solana","SOL","address_transaction",stamp,now,0,"transaction",None,address,transaction_id=signature,block_number=int(slot) if slot is not None else None,finality="finalized",metadata={"fee_lamports":meta.get("fee"),"success":meta.get("err") is None}))
                newest=signature
        return events,newest
