from types import SimpleNamespace
import pytest
from aitos.intelligence.staged_scanner import install_staged_scan

class LiveUniverse:
 async def rank(self):
  return [(f"S{i}",float(100-i)) for i in range(60)]

class NoHistory:
 async def analyze(self,*args,**kwargs):
  return None,{"available":False,"reason":"not_available","evidence_only":True}
 async def persist(self,*args,**kwargs):
  return None

class ExchangeMustNotFetch:
 async def fetch_klines(self,*args,**kwargs):
  raise AssertionError("universe-wide candle fetch must not occur")

class Scanner:
 def __init__(self):
  self._staged_scan_installed=False
  self._symbols=[f"S{i}" for i in range(60)]
  self._reference_symbol="S0"
  self._timeframe="15m"
  self._kline_lookback=80
  self._exchange=ExchangeMustNotFetch()
  self._aitos_universe_market_context=LiveUniverse()
  self._aitos_historical_intelligence=NoHistory()
  self.scan_calls=[]
 async def scan_symbol(self,symbol,reference_klines):
  assert reference_klines is None
  self.scan_calls.append(symbol)
  return SimpleNamespace(symbol=symbol,composite_score=float(symbol[1:]))

@pytest.mark.asyncio
async def test_live_universe_path_uses_tickers_then_deep_scans_top_ten_only():
 scanner=Scanner(); stages=[]
 async def changed(symbols,stage): stages.append((stage,list(symbols)))
 await install_staged_scan(scanner,changed)
 result=await scanner.scan_all()
 assert len(scanner.scan_calls)==10
 assert len(result)==2
 assert [s for s,_ in stages]==["TOP_50","TOP_25","TOP_10","TOP_5","TOP_2"]
 assert stages[0][1][0]=="S0"
 assert stages[-1][1]==["S9","S8"]
