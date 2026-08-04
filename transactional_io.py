"""Atomic file writers shared by CSV and Pandas-producing loaders."""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal


def _temporary_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(raw_path)


def _sync_file(path: Path) -> None:
    # Windows requires a writable descriptor for fsync(). Pandas has already
    # closed the file at this point, so reopen it read/write before syncing.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def atomic_write_csv(
    path: str | os.PathLike[str],
    fieldnames: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    encoding: str = "utf-8-sig",
    lineterminator: str = "\n",
    quoting: Literal[0, 1, 2, 3] = csv.QUOTE_MINIMAL,
) -> None:
    """Write and fsync a complete CSV before replacing its destination."""

    destination = Path(path)
    columns = list(fieldnames)
    if not columns:
        raise ValueError(f"cannot write headerless CSV: {destination}")
    temporary = _temporary_path(destination)
    try:
        with temporary.open("w", newline="", encoding=encoding) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=columns,
                lineterminator=lineterminator,
                quoting=quoting,
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_dataframe(
    dataframe: Any,
    path: str | os.PathLike[str],
    **to_csv_options: Any,
) -> None:
    """Write a Pandas-compatible dataframe to a same-directory temp file."""

    destination = Path(path)
    temporary = _temporary_path(destination)
    try:
        dataframe.to_csv(temporary, **to_csv_options)
        _sync_file(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
