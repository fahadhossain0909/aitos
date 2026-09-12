"""Protect all currently open positions from scanner subscription churn."""
from __future__ import annotations
from functools import wraps
from typing import Any, Callable
from aitos.logging_setup import get_logger
logger=get_logger('aitos.data.protected_position_monitor')
_provider: Callable[[], list[Any]] | None = None
def set_open_position_provider(provider: Callable[[], list[Any]]) -> None:
    global _provider
    _provider=provider
def _protected() -> set[str]:
    if _provider is None: return set()
    try: return {str(getattr(t,'symbol','')).upper() for t in _provider() if getattr(t,'symbol',None)}
    except Exception:
        logger.exception('open-position provider failed'); return set()
def install_protected_position_monitor() -> None:
    from aitos.data.ingestion import DataIngestionService
    if getattr(DataIngestionService,'_protected_position_monitor_installed',False): return
    original_trade=DataIngestionService.update_live_trade_symbols
    original_kline=DataIngestionService.update_live_kline_symbols
    @wraps(original_trade)
    async def trade(self: Any, symbols):
        requested=[str(s).upper() for s in symbols if s]; protected=sorted(_protected())
        return await original_trade(self,list(dict.fromkeys([*requested,*protected])))
    @wraps(original_kline)
    async def kline(self: Any, symbols):
        requested=[str(s).upper() for s in symbols if s]; protected=sorted(_protected())
        return await original_kline(self,list(dict.fromkeys([*requested,*protected])))
    DataIngestionService.update_live_trade_symbols=trade
    DataIngestionService.update_live_kline_symbols=kline
    DataIngestionService._protected_position_monitor_installed=True
