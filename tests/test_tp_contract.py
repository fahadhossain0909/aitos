from aitos.models.trade import TradeSide
from aitos.trading.tp_contract import TakeProfitExecutionContract


def test_intelligent_tp_levels_are_checkpoints_not_auto_close_orders():
    contract = TakeProfitExecutionContract(intelligent=True)
    assert contract.tp_is_checkpoint is True
    assert contract.static_tp_may_close is False
    assert contract.reached(TradeSide.LONG, 110.0, 110.0)
    assert contract.reached(TradeSide.SHORT, 90.0, 90.0)


def test_legacy_tp_contract_preserves_static_execution_authority():
    contract = TakeProfitExecutionContract(intelligent=False)
    assert contract.tp_is_checkpoint is False
    assert contract.static_tp_may_close is True


def test_dynamic_tp_extension_must_move_target_further_in_trade_direction():
    contract = TakeProfitExecutionContract(intelligent=True)
    assert contract.can_extend(TradeSide.LONG, 110.0, 115.0)
    assert not contract.can_extend(TradeSide.LONG, 110.0, 105.0)
    assert contract.can_extend(TradeSide.SHORT, 90.0, 85.0)
    assert not contract.can_extend(TradeSide.SHORT, 90.0, 95.0)
    assert not contract.can_extend(TradeSide.LONG, 110.0, 0.0)
