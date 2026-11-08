"""Execute actual runner teardown AST without constructing a trading system."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


@pytest.mark.parametrize(
    "failure", ["none", "heal", "health", "persistence", "repository"]
)
async def test_real_runner_finally_always_drains_new_shadow_worker(failure):
    tree = ast.parse(Path("run_paper_trading.py").read_text())
    runner = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_paper_trading"
    )
    main_try = next(
        n for n in reversed(runner.body) if isinstance(n, ast.Try) and n.finalbody
    )
    env = {}

    async def fail():
        raise OSError("synthetic teardown failure")

    def obj(**kwargs):
        return SimpleNamespace(**kwargs)

    env.update(
        _cancel_task=AsyncMock(
            side_effect=OSError("synthetic") if failure == "heal" else None
        ),
        heal_task=object(),
        _stop_learning_worker=AsyncMock(),
        learning_worker=object(),
        learning_task=object(),
        price_safety_net=obj(stop=AsyncMock()),
        _shutdown_agents=AsyncMock(),
        agents=[],
        tracker=obj(persist_state=AsyncMock()),
        components=obj(trade_lifecycle=object()),
        asyncio=obj(to_thread=AsyncMock()),
        rl_scorer=obj(save_state=lambda: None),
        outcome_classifier=obj(save_state=lambda: None),
        save_attention_model=lambda: None,
        attention_explainer=object(),
        attention_path="synthetic",
        health_server=obj(
            stop=AsyncMock(
                side_effect=OSError("synthetic") if failure == "health" else None
            )
        ),
        experience_recorder=obj(shutdown=AsyncMock()),
        market_os_persistence=obj(shutdown=AsyncMock()),
        trade_state_persistence=obj(
            shutdown=AsyncMock(
                side_effect=OSError("synthetic") if failure == "persistence" else None
            )
        ),
        shadow=obj(stop=AsyncMock()),
        shutdown_all=AsyncMock(),
        market_repo=obj(
            shutdown=AsyncMock(
                side_effect=OSError("synthetic") if failure == "repository" else None
            )
        ),
        journal_repo=obj(shutdown=AsyncMock()),
        graph_driver=None,
        redis_client=obj(close=AsyncMock()),
    )
    # The production finalbody is executed as-is, not a reimplementation.
    function = ast.AsyncFunctionDef(
        name="teardown",
        args=ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
        ),
        body=main_try.finalbody,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, "actual-runner-teardown", "exec"), env)
    if failure == "none":
        await env["teardown"]()
    else:
        with pytest.raises(OSError):
            await env["teardown"]()
    assert env["shadow"].stop.await_count >= 1
