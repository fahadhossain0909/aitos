"""Temporary, reason-aware candidate cooldowns for scanner rotation."""
from __future__ import annotations
import time
from collections import OrderedDict
from dataclasses import dataclass

@dataclass(frozen=True)
class _Cooldown:
    expires_at: float
    fingerprint: str
    reason: str

class CandidateRotationManager:
    """Cooldowns reduce repeated compute; they never change ranking or risk rules."""
    def __init__(self, *, default_cooldown_seconds=15.0, kernel_cooldown_seconds=20.0,
                 market_change_recheck_pct=0.15, max_symbols=5000):
        self.default_cooldown_seconds=max(0.0,default_cooldown_seconds)
        self.kernel_cooldown_seconds=max(0.0,kernel_cooldown_seconds)
        self.market_change_recheck_pct=max(0.0,market_change_recheck_pct)
        self.max_symbols=max(100,max_symbols)
        self._cooldowns=OrderedDict()
        self._rejections=self._cooldown_skips=self._early_rechecks=self._expirations=self._forced_rechecks=0

    def _duration(self, reason):
        if reason=="kernel_rejection": return self.kernel_cooldown_seconds
        if reason in {"lifecycle_rejection","quality_rejection"}: return self.default_cooldown_seconds
        return 0.0  # portfolio/capital constraints are not symbol-level market failures

    def record_rejection(self, symbol, reason, fingerprint):
        self._rejections+=1
        key=symbol.upper()
        duration=self._duration(reason)
        if duration<=0:
            self._cooldowns.pop(key,None)
            return
        self._cooldowns[key]=_Cooldown(time.monotonic()+duration,str(fingerprint),reason)
        self._cooldowns.move_to_end(key)
        while len(self._cooldowns)>self.max_symbols: self._cooldowns.popitem(last=False)

    def _materially_changed(self, old, new):
        if old==new or new in {"unavailable", "unscored"}: return False
        try:
            a,b=old.split(":"),new.split(":")
            op,np=float(a[0]),float(b[0])
            ore,nre=float(a[1]),float(b[1])
            oa,na=float(a[2]),float(b[2])
        except (ValueError,IndexError):
            return old!=new
        price=abs(np/op-1)*100 if op>0 else 0
        ret=abs(nre-ore)*100
        activity=abs(na/oa-1)*100 if oa>0 else (100 if na>0 else 0)
        return max(price,ret,activity)>=self.market_change_recheck_pct

    def should_skip(self, symbol, fingerprint):
        key=symbol.upper()
        state=self._cooldowns.get(key)
        if state is None: return False
        if time.monotonic()>=state.expires_at:
            self._cooldowns.pop(key,None); self._expirations+=1; return False
        if self._materially_changed(state.fingerprint,str(fingerprint)):
            self._cooldowns.pop(key,None); self._early_rechecks+=1; return False
        self._cooldowns.move_to_end(key); self._cooldown_skips+=1
        return True

    def force_recheck(self,symbol):
        if self._cooldowns.pop(symbol.upper(),None) is not None: self._forced_rechecks+=1

    def snapshot(self):
        now=time.monotonic()
        expired=[k for k,v in self._cooldowns.items() if v.expires_at<=now]
        for k in expired: self._cooldowns.pop(k,None)
        self._expirations+=len(expired)
        return {"active_cooldowns":len(self._cooldowns),"rejections":self._rejections,
                "cooldown_skips":self._cooldown_skips,"early_rechecks":self._early_rechecks,
                "forced_rechecks":self._forced_rechecks,"expirations":self._expirations}
