"""Immutable prepared-publication inputs and outcome-free research observations.

Replay uses captured inputs, never a collector, model, or latest archive. A
prepared record does not attest delivery or authorize return evaluation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from src.util.point_in_time import evidence
from src.util.session import next_trading_day, now_utc, session_open_utc, to_utc


def _clean(value):
    if isinstance(value, np.generic):
        return _clean(value.item())
    if is_dataclass(value):
        return _clean(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    if isinstance(value, (dt.date, pd.Timestamp, Path)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json(value) -> bytes:
    return json.dumps(_clean(value), sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _blob(directory: Path, content: bytes, suffix: str) -> str:
    name = hashlib.sha256(content).hexdigest() + suffix
    target = directory / name
    if not target.exists():
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            pass  # Another writer owns the same content hash; verify below.
    if target.read_bytes() != content:
        raise ValueError(f"Corrupt publication blob: {name}")
    return name


def _read_blob(directory: Path, name: str) -> bytes:
    if Path(name).name != name:
        raise ValueError("Invalid publication blob path")
    content = (directory / name).read_bytes()
    if hashlib.sha256(content).hexdigest() != name.split(".")[0]:
        raise ValueError(f"Publication hash mismatch: {name}")
    return content


def write_publication(inputs, results, *, report: str, run: str) -> Path:
    """Pin selected inputs and one observation per ticker without outcomes."""
    from src.features.compute import z_scores_for
    from src.report.render import render_header

    if run not in {"morning", "evening", "manual"}:
        raise ValueError("Unknown publication slot")
    inputs.as_of = to_utc(inputs.as_of)
    directory = inputs.root / "publications"
    blobs = directory / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    captured = {}
    for item in fields(inputs):
        value = getattr(inputs, item.name)
        if isinstance(value, pd.DataFrame):
            captured[item.name] = {
                "frame_blob": _blob(blobs, value.to_parquet(index=False), ".parquet")
            }
        elif item.name != "root":
            captured[item.name] = _clean(value)
    project = Path(__file__).resolve().parents[2]
    identity_files = sorted(
        [
            *project.glob("src/**/*.py"),
            *project.glob("scripts/*.py"),
            *project.glob("config/*.yaml"),
            *project.glob("src/llm/prompts/*.md"),
            project / "pyproject.toml",
        ]
    )
    identity = [
        {**evidence(path), "path": str(path.relative_to(project))} for path in identity_files
    ]
    core_sources = {"kr/investor_flow", "kr/price"}
    core_evidence = [row for row in inputs.provenance if row.get("source") in core_sources]
    reasons = []
    if {row["source"] for row in core_evidence} != core_sources:
        reasons.append("missing_core_source_evidence")
    if any(row.get("legacy") for row in core_evidence):
        reasons.append("unknown_core_ingestion")
    policy_id = hashlib.sha256(_json(inputs.rating_config)).hexdigest()
    code_config_id = hashlib.sha256(_json(identity)).hexdigest()
    publication_id = f"{inputs.day}-{run}-{uuid4().hex}"
    recorded_at = now_utc()
    recorded = recorded_at.isoformat()
    if run == "evening":
        next_open = session_open_utc("KR", next_trading_day("KR", inputs.day))
        if not (inputs.as_of <= recorded_at < next_open):
            reasons.append("not_recorded_before_next_open")
    observations = []
    for ticker, result in sorted(results.items()):
        z = z_scores_for(inputs.features, ticker, inputs.day)
        incomplete = any(
            z.get(name) is None
            for name in (
                "foreign_flow_5d",
                "inst_flow_5d",
                "short_ratio",
                "rel_strength_20d",
                "valuation_band",
            )
        )
        observations.append(
            {
                "publication_id": publication_id,
                "ticker": ticker,
                "session": inputs.day.isoformat(),
                "cutoff_utc": inputs.as_of.isoformat(),
                "recorded_at_utc": recorded,
                "available_at_utc": recorded,
                "run": run,
                "prediction_kind": "frozen_deterministic_rating",
                "prediction": result.score,
                "model_id": "deterministic_rating_v1",
                "policy_id": policy_id,
                "code_config_id": code_config_id,
                "missing_features": [name for name, value in z.items() if value is None],
                "weight_coverage": result.weight_coverage,
                "features": z,
                "rating": _clean(result),
                "research_eligible": not reasons
                and not incomplete
                and not result.low_confidence
                and run == "evening",
                "ineligible_reasons": reasons
                + (["incomplete_features"] if incomplete or result.low_confidence else [])
                + (["not_evening_slot"] if run != "evening" else []),
            }
        )
    manifest = {
        "schema_version": 1,
        "publication_id": publication_id,
        "state": "prepared",
        "recorded_at_utc": recorded,
        "cutoff_utc": inputs.as_of.isoformat(),
        "inputs": captured,
        "source_evidence": inputs.provenance,
        "code_config_identity": identity,
        "code_config_id": code_config_id,
        "policy_id": policy_id,
        "reference_sessions": {
            name: str(pd.to_datetime(frame["date"]).max().date())
            for name in ("kr_prices", "us_prices", "macro")
            if not (frame := getattr(inputs, name)).empty and "date" in frame
        },
        "selected_score_ids": inputs.news_scores.get("score_id", pd.Series(dtype="object"))
        .dropna()
        .tolist(),
        "ratings": _clean(results),
        "header": render_header(inputs, extra_warnings=inputs.publication_warnings),
        "report_blob": _blob(blobs, report.encode(), ".md"),
        "observations_blob": _blob(blobs, _json(observations), ".json"),
        "legacy_provenance": any(row.get("legacy") for row in inputs.provenance),
    }
    target = directory / f"{publication_id}.json"
    with target.open("xb") as handle:
        handle.write(_json(manifest))
    return target


def write_delivery_receipt(publication: Path, delivery_results) -> Path:
    """Record channel acceptance after dispatch; never claim inbox receipt."""
    manifest = json.loads(publication.read_text())
    directory = publication.parent / "receipts"
    directory.mkdir(exist_ok=True)
    target = directory / f"{manifest['publication_id']}-{uuid4().hex}.json"
    with target.open("xb") as handle:
        handle.write(
            _json(
                {
                    "schema_version": 1,
                    "publication_id": manifest["publication_id"],
                    "manifest_sha256": evidence(publication)["sha256"],
                    "dispatch_completed_at_utc": now_utc().isoformat(),
                    "channels": delivery_results,
                    "inbox_receipt_confirmed": False,
                }
            )
        )
    return target


def replay_publication(path: Path) -> dict:
    """Validate pinned ratings/header without delivery, LLMs or outcomes.

    The current evaluator must have identical code/config file contents.
    A different checkout is rejected instead of silently asserting equivalence.
    """
    import io

    from src.report.render import ReportInputs, rate_all, render_header
    from src.util.config import AliasEntry, WatchlistEntry

    manifest = json.loads(path.read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported publication schema")
    project = Path(__file__).resolve().parents[2]
    for row in manifest["code_config_identity"]:
        if evidence(project / row["path"])["sha256"] != row["sha256"]:
            raise ValueError(f"Replay code/config changed: {row['path']}")
    blobs = path.parent / "blobs"
    captured = manifest["inputs"].copy()
    for name, value in captured.items():
        if isinstance(value, dict) and "frame_blob" in value:
            captured[name] = pd.read_parquet(io.BytesIO(_read_blob(blobs, value["frame_blob"])))
    captured["day"] = dt.date.fromisoformat(captured["day"])
    captured["as_of"] = pd.Timestamp(captured["as_of"])
    captured["watchlist"] = [WatchlistEntry(**entry) for entry in captured["watchlist"]]
    captured["aliases"] = {key: AliasEntry(**entry) for key, entry in captured["aliases"].items()}
    captured["us_preview_dates"] = [
        dt.date.fromisoformat(day) for day in captured["us_preview_dates"]
    ]
    inputs = ReportInputs(**captured)
    ratings = _clean(rate_all(inputs))
    if ratings != manifest["ratings"]:
        raise ValueError("Pinned ratings differ")
    if render_header(inputs, extra_warnings=inputs.publication_warnings) != manifest["header"]:
        raise ValueError("Pinned warnings/header differ")
    _read_blob(blobs, manifest["report_blob"])
    observations = json.loads(_read_blob(blobs, manifest["observations_blob"]))
    if {row["ticker"] for row in observations} != set(ratings) or len(observations) != len(ratings):
        raise ValueError("Observation universe differs from pinned ratings")
    for row in observations:
        if (
            row["publication_id"] != manifest["publication_id"]
            or row["cutoff_utc"] != manifest["cutoff_utc"]
            or row["rating"] != ratings[row["ticker"]]
        ):
            raise ValueError("Observation differs from pinned publication")
    return {
        "publication_id": manifest["publication_id"],
        "ratings": len(ratings),
        "observations": len(observations),
        "verified": True,
        "legacy_provenance": manifest["legacy_provenance"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one pinned publication offline.")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay_publication(args.manifest), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
