import inspect

from aitos.trading.lifecycle import TradeLifecycle


def test_tmp_lifecycle_capital_diagnostic():
    fn = TradeLifecycle.submit_opportunity
    print("SUBMIT_VARS", fn.__code__.co_varnames)
    print("SUBMIT_FREEVARS", fn.__code__.co_freevars)
    print("SUBMIT_NAMES", fn.__code__.co_names)
    print("SUBMIT_GLOBAL_AUTHORIZE", inspect.signature(fn.__globals__["authorize_opportunity"]))
    print("SUBMIT_GLOBAL_AUTHORIZE_OBJ", fn.__globals__["authorize_opportunity"])
    raise AssertionError("DIAGNOSTIC_STOP")
