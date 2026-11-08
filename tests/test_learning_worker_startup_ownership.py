"""Learning startup failures must not leak a database client or owner lock."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aitos.learning.worker import ContinualLearningWorker


@pytest.mark.parametrize("stage", ["scorer", "model", "lock", "cursor"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_constructor_failure_closes_client_releases_lock_and_preserves_cause(
    tmp_path, monkeypatch, stage, close_fails
):
    failure = RuntimeError("injected startup failure")
    client = SimpleNamespace(
        close=Mock(side_effect=OSError("close failed") if close_fails else None)
    )
    scorer = SimpleNamespace(load_state=Mock())
    create_scorer = Mock(return_value=scorer)
    acquire = Mock()
    release = Mock()
    load_cursor = Mock()
    target = {
        "scorer": create_scorer,
        "model": scorer.load_state,
        "lock": acquire,
        "cursor": load_cursor,
    }[stage]
    target.side_effect = failure
    monkeypatch.setattr(
        "aitos.learning.worker.clickhouse_connect.get_client", Mock(return_value=client)
    )
    monkeypatch.setattr("aitos.learning.worker.DeepValueRLScorer", create_scorer)
    monkeypatch.setattr(ContinualLearningWorker, "_acquire_singleton_lock", acquire)
    monkeypatch.setattr(ContinualLearningWorker, "_release_singleton_lock", release)
    monkeypatch.setattr(ContinualLearningWorker, "_load_state", load_cursor)
    with pytest.raises(RuntimeError) as caught:
        ContinualLearningWorker(
            state_path=str(tmp_path / "state.json"),
            model_path=str(tmp_path / "model.pkl"),
        )
    assert caught.value is failure
    client.close.assert_called_once()
    release.assert_called_once()


@pytest.mark.parametrize("fails", [False, True])
def test_once_entrypoint_always_releases_worker(monkeypatch, fails):
    import run_continual_learning as runner

    worker = SimpleNamespace(run_once=Mock(), close=Mock())
    failure = RuntimeError("injected batch failure")
    if fails:
        worker.run_once.side_effect = failure
    monkeypatch.setattr("sys.argv", ["run_continual_learning.py", "--once"])
    monkeypatch.setattr(runner, "ContinualLearningWorker", Mock(return_value=worker))
    if fails:
        with pytest.raises(RuntimeError) as caught:
            runner.main()
        assert caught.value is failure
    else:
        assert runner.main() == 0
    worker.close.assert_called_once()
