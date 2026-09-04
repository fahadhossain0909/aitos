from aitos.trading.lifecycle import TradeLifecycle


def test_tmp_lifecycle_capital_diagnostic():
    fn = TradeLifecycle.submit_opportunity
    print("SUBMIT_NAMES", fn.__code__.co_names)
    print("SUBMIT_CONSTS", fn.__code__.co_consts)
    raise AssertionError("DIAGNOSTIC_STOP")
