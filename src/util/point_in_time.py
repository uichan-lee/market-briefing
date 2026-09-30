"""Availability selection shared by features and display loaders."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

import pandas as pd

from src.util.session import now_utc, to_utc

INGESTED = "ingested_at_utc"


def parse_clocks(values: pd.Series) -> pd.Series:
    """Normalize aware ISO clocks; naive, invalid and numeric values are unknown.

    ISO8601 accepts mixed precision without inferring a format from the first
    row. Check awareness before utc=True can silently localize naive inputs.
    """
    if isinstance(values.dtype, pd.DatetimeTZDtype):
        return pd.to_datetime(values, utc=True, errors="coerce")

    def aware(value) -> bool:
        if value is pd.NaT:
            return False
        if isinstance(value, str):
            return re.search(r"[Tt ].*(?:Z|[+-]\d{2}:?\d{2})$", value) is not None
        return isinstance(value, dt.datetime) and value.utcoffset() is not None

    return pd.to_datetime(
        values.where(values.map(aware)), format="ISO8601", utc=True, errors="coerce"
    )


def unknown_clocks(frame: pd.DataFrame, clocks: tuple[str, ...]) -> bool:
    """Use the selector's clock validity for provenance disclosure too."""
    return any(column not in frame or parse_clocks(frame[column]).isna().any() for column in clocks)


def stamp_ingestion(frame: pd.DataFrame, at: pd.Timestamp | None = None) -> pd.DataFrame:
    """Record this write's clock, never an inferred historical release clock."""
    out = frame.copy()
    out[INGESTED] = to_utc(at if at is not None else now_utc())
    return out


def evidence(path: Path) -> dict:
    content = path.read_bytes()
    return {"path": str(path), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def visible_versions(
    frame: pd.DataFrame,
    as_of: pd.Timestamp | None,
    *,
    strict: bool = False,
    clocks: tuple[str, ...] = ("known_at_utc", INGESTED),
) -> pd.DataFrame:
    """Filter before de-duplication. Legacy unknown clocks remain disclosed.

    Operational display can retain legacy rows; strict research excludes them.
    Known clocks always constrain availability, including in permissive mode.
    """
    if strict and as_of is None:
        raise ValueError("Strict availability selection requires an explicit cutoff")
    if frame.empty:
        return frame
    out = frame.copy()
    cutoff = to_utc(as_of) if as_of is not None else None
    mask = pd.Series(True, index=frame.index)
    for column in clocks:
        times = (
            parse_clocks(frame[column])
            if column in frame
            else pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
        )
        if column in out:
            out[column] = times
        if cutoff is not None:
            mask &= times.lt(cutoff) | (times.isna() & (not strict))
    return out.loc[mask].copy()
