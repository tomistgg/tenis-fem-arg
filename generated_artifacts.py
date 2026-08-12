"""Definitions and cleanup for browser artifacts that do not belong in Git data."""

from __future__ import annotations

import argparse
from pathlib import Path

GENERATED_DATA_PATTERNS = (
    "history_data_bundle.js",
    "player_aliases_wta_itf_bundle.js",
    "wta_rankings_latest_bundle.js",
    "wta_rankings_[0-9][0-9][0-9][0-9]_bundle.js",
)


def remove_generated_data_artifacts(data_dir: str | Path) -> list[Path]:
    """Remove only known generated browser bundles from a canonical data tree."""

    data_dir = Path(data_dir).resolve()
    removed = []
    for pattern in GENERATED_DATA_PATTERNS:
        for path in data_dir.glob(pattern):
            if path.is_file() and path.parent.resolve() == data_dir:
                path.unlink()
                removed.append(path)
    return sorted(set(removed))


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove generated bundles from a canonical data directory.")
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()
    removed = remove_generated_data_artifacts(args.data_dir)
    print(f"Removed {len(removed)} generated data artifacts from {Path(args.data_dir).resolve()}.")


if __name__ == "__main__":
    main()
