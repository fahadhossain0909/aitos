import pytest
from aitos.capital_intelligence.binance_market import BinanceMarketStructureAdapter

class Response:
    status=200
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    def raise_for_status(self): pass
    async def json(self): return []
class Session:
    closed=False
    def get(self,*args,**kwargs): return Response()

def test_adapter_validates_symbol_and_threshold():
    with pytest.raises(ValueError):
        BinanceMarketStructureAdapter(symbols=[])
    with pytest.raises(ValueError):
        BinanceMarketStructureAdapter(symbols=["BTCUSDT"], min_trade_notional_usd=0)
    adapter=BinanceMarketStructureAdapter(symbols=["btcusdt"],session=Session())
    assert adapter.symbols == ["BTCUSDT"]
