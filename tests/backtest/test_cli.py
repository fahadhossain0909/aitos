import json
from pathlib import Path

from aitos.backtest.cli import main


def test_cli_runs_jsonl_backtest(tmp_path: Path, capsys):
    dataset = tmp_path / "events.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "price": 100.0},
        {"timestamp": "2026-01-01T00:00:01+00:00", "price": 110.0},
    ]
    dataset.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    assert main(["--data", str(dataset), "--fee-rate", "0"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["events"] == 2
    assert output["final_equity"] == 10010.0
    assert output["total_return"] == 0.001
