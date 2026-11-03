"""Safe local persistence helpers for the online attention explainer."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any


def save_attention_model(model: Any, path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("wb") as handle:
        pickle.dump(model, handle, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(target)


def load_attention_model(path: str) -> Any | None:
    target = Path(path)
    if not target.exists():
        return None
    with target.open("rb") as handle:
        return pickle.load(
            handle
        )  # nosec B301 - state is generated and stored locally by AITOS
