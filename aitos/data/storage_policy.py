"""Storage lifecycle policy for historical market data.

Parquet/ZSTD is the long-term canonical format. Raw archives and extracted
intermediate files are temporary unless explicitly retained.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoragePolicy:
    keep_raw_archives: bool = False
    keep_extracted: bool = False
    require_parquet_before_cleanup: bool = True


class StorageLifecycle:
    def __init__(self, policy: StoragePolicy = StoragePolicy()):
        self.policy = policy

    @staticmethod
    def _has_parquet(root: Path) -> bool:
        return any(root.rglob("*.parquet"))

    def cleanup(
        self,
        raw_archive: str | Path | None,
        extracted_dir: str | Path | None,
        parquet_root: str | Path,
    ) -> list[Path]:
        """Remove temporary ingestion files after canonical data exists."""
        parquet_root = Path(parquet_root)
        if self.policy.require_parquet_before_cleanup and not self._has_parquet(
            parquet_root
        ):
            raise RuntimeError(
                "Refusing cleanup: no canonical Parquet output was found"
            )

        removed: list[Path] = []
        if not self.policy.keep_extracted and extracted_dir:
            path = Path(extracted_dir)
            if path.exists():
                shutil.rmtree(path)
                removed.append(path)

        if not self.policy.keep_raw_archives and raw_archive:
            path = Path(raw_archive)
            if path.exists():
                path.unlink()
                removed.append(path)
        return removed
