"""Guard asyncio.wait_for handlers against Python 3.10 exception semantics."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_paper_and_live_shutdown_waits_catch_asyncio_timeout_error():
    for name in ("run_paper_trading.py", "run_live_trading.py"):
        source = (ROOT / name).read_text(encoding="utf-8")
        assert "await asyncio.wait_for(" in source
        assert "except asyncio.TimeoutError:" in source
