"""The four checks every collector must pass.

CLAUDE.md requires all four, written *before* the fetching logic, and states
that a collector without them does not get merged:

1. Assert the returned schema — column names and dtypes.
2. Check the missing-value ratio against a declared threshold.
3. Check trading-day continuity, excluding market holidays.
4. Compare at least one known value against a hardcoded expected result.

Results are **reported, not just raised.** CLAUDE.md also requires that a failing
collector record the failure and let the pipeline continue, and that missing data
appear in the report header rather than only in logs. A framework that could only
raise would make partial-report publication impossible, so each check returns a
:class:`CheckResult` and the aggregate :class:`ValidationReport` is designed to be
rendered into the briefing header. Callers that genuinely want to abort — tests,
mostly — call :meth:`ValidationReport.raise_if_failed`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, pandas_dtype

from src.util.session import Market, trading_days


class ValidationFailedError(RuntimeError):
    """Raised by :meth:`ValidationReport.raise_if_failed` when a check failed."""


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single check."""

    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'ok' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclass
class ValidationReport:
    """Aggregate outcome for one collector run."""

    collector: str
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> CheckResult:
        self.results.append(result)
        return result

    @property
    def ok(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        """One line, suitable for the briefing header (SPEC §2.1)."""
        if not self.results:
            return f"{self.collector}: no checks run"
        if self.ok:
            return f"{self.collector}: {len(self.results)} checks passed"
        names = ", ".join(r.name for r in self.failures)
        return f"{self.collector}: {len(self.failures)}/{len(self.results)} checks FAILED ({names})"

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        detail = "\n".join(str(r) for r in self.failures)
        raise ValidationFailedError(f"{self.collector} validation failed:\n{detail}")


# --- check 1: schema -----------------------------------------------------


def _dtype_matches(actual, expected: str) -> bool:
    """Whether ``actual`` satisfies the declared ``expected`` dtype.

    Exact equality, with one deliberate exception: datetime64 resolution is
    ignored. pandas picks the unit from how a column happened to be built
    (``date`` objects give ``[s]``, strings give ``[us]``, epoch ints give
    ``[ns]``), which is a storage detail rather than a data-correctness one.
    Failing a collector over it would generate noise that trains the reader to
    ignore the validator — the worst outcome for a project whose stated
    bottleneck is data correctness.

    Timezone-awareness is still compared strictly, since CLAUDE.md requires
    everything be stored in UTC and a naive timestamp is a real defect.
    """
    want = pandas_dtype(expected)
    if actual == want:
        return True
    if is_datetime64_any_dtype(actual) and is_datetime64_any_dtype(want):
        return getattr(actual, "tz", None) == getattr(want, "tz", None)
    return False


def check_schema(df: pd.DataFrame, expected: Mapping[str, str]) -> CheckResult:
    """Assert column names and dtypes.

    ``expected`` maps column name to a pandas dtype string, e.g.
    ``{"date": "datetime64[s]", "ticker": "str", "close": "float64"}``.

    Note that under pandas 3.x a text column is ``str``, not ``object`` — that
    changed in pandas 3.0 and is an easy thing to get wrong when copying a
    schema from older reference material.

    Extra columns are reported too: an unannounced new column from an upstream
    API is exactly the kind of silent change worth catching.
    """
    problems: list[str] = []

    missing = [c for c in expected if c not in df.columns]
    if missing:
        problems.append(f"missing columns {missing}")

    extra = [c for c in df.columns if c not in expected]
    if extra:
        problems.append(f"unexpected columns {extra}")

    for column, want in expected.items():
        if column not in df.columns:
            continue
        actual = df[column].dtype
        if not _dtype_matches(actual, want):
            problems.append(f"{column} dtype is {actual}, expected {want}")

    if problems:
        return CheckResult("schema", False, "; ".join(problems))
    return CheckResult("schema", True, f"{len(expected)} columns match")


# --- check 2: missing values ---------------------------------------------


def check_missing_ratio(df: pd.DataFrame, thresholds: Mapping[str, float]) -> CheckResult:
    """Check each column's missing-value ratio against its declared threshold.

    ``thresholds`` maps column name to the maximum tolerated ratio in ``[0, 1]``.
    A threshold of ``0.0`` means the column must be complete.
    """
    if df.empty:
        return CheckResult("missing_ratio", False, "frame is empty; no rows to check")

    problems: list[str] = []
    checked = 0

    for column, limit in thresholds.items():
        if column not in df.columns:
            problems.append(f"{column} absent")
            continue
        ratio = float(df[column].isna().mean())
        checked += 1
        if ratio > limit:
            problems.append(f"{column} missing {ratio:.1%} > {limit:.1%} allowed")

    if problems:
        return CheckResult("missing_ratio", False, "; ".join(problems))
    return CheckResult("missing_ratio", True, f"{checked} columns within threshold")


# --- check 3: trading-day continuity -------------------------------------


def check_trading_day_continuity(
    df: pd.DataFrame,
    market: Market,
    date_column: str,
    start: dt.date,
    end: dt.date,
) -> CheckResult:
    """Check that every trading day in ``[start, end]`` is present exactly once.

    Market holidays are excluded via the calendar, never a hardcoded list. Both
    directions are checked: a gap means the collector lost a day, and a date the
    market was closed means it invented one.
    """
    if date_column not in df.columns:
        return CheckResult("trading_day_continuity", False, f"{date_column} absent")

    expected = set(trading_days(market, start, end))
    if not expected:
        return CheckResult(
            "trading_day_continuity", False, f"no {market} trading days in {start}..{end}"
        )

    present = pd.to_datetime(df[date_column])
    in_range = [d.date() for d in present if start <= d.date() <= end]

    problems: list[str] = []

    gaps = sorted(expected - set(in_range))
    if gaps:
        shown = ", ".join(str(d) for d in gaps[:5])
        suffix = f" (+{len(gaps) - 5} more)" if len(gaps) > 5 else ""
        problems.append(f"{len(gaps)} missing trading days: {shown}{suffix}")

    non_trading = sorted(set(in_range) - expected)
    if non_trading:
        shown = ", ".join(str(d) for d in non_trading[:5])
        problems.append(f"{len(non_trading)} rows on non-trading days: {shown}")

    counts = pd.Series(in_range).value_counts()
    duplicated = sorted(counts[counts > 1].index)
    if duplicated:
        shown = ", ".join(str(d) for d in duplicated[:5])
        problems.append(f"{len(duplicated)} duplicated dates: {shown}")

    if problems:
        return CheckResult("trading_day_continuity", False, "; ".join(problems))
    return CheckResult(
        "trading_day_continuity", True, f"{len(expected)} trading days present, no gaps"
    )


# --- check 4: known value ------------------------------------------------


def check_known_value(
    df: pd.DataFrame,
    where: Mapping[str, object],
    column: str,
    expected: float,
    tolerance: float = 0.0,
) -> CheckResult:
    """Compare one hardcoded known value against the collected data.

    The cheapest guard against a collector that returns well-formed but wrong
    numbers — a shifted date index, a different price field, a units change.
    ``where`` selects exactly one row, e.g.
    ``{"date": date(2026, 1, 5), "ticker": "005930"}``.
    """
    label = ", ".join(f"{k}={v!r}" for k, v in where.items())

    if column not in df.columns:
        return CheckResult("known_value", False, f"{column} absent")

    mask = pd.Series(True, index=df.index)
    for key, value in where.items():
        if key not in df.columns:
            return CheckResult("known_value", False, f"selector column {key} absent")
        series = df[key]
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            series = pd.to_datetime(series).dt.date
        mask &= series == value

    matched = df[mask]
    if len(matched) != 1:
        return CheckResult(
            "known_value", False, f"selector ({label}) matched {len(matched)} rows, expected 1"
        )

    actual = float(matched[column].iloc[0])
    # NaN before the tolerance comparison, because `nan > tolerance` is False and
    # a null at the pinned row would otherwise report *passed* — the one outcome
    # this check exists to make impossible. `inf` needs no special case: it
    # compares greater than any tolerance and fails on its own.
    if pd.isna(actual):
        return CheckResult(
            "known_value", False, f"{column} at ({label}) is null, expected {expected}"
        )
    delta = abs(actual - expected)
    if delta > tolerance:
        return CheckResult(
            "known_value",
            False,
            f"{column} at ({label}) is {actual}, expected {expected} (±{tolerance})",
        )
    return CheckResult("known_value", True, f"{column} at ({label}) == {actual}")


# --- aggregate -----------------------------------------------------------


def validate(collector: str, checks: Sequence[CheckResult]) -> ValidationReport:
    """Bundle check results into a report for one collector run."""
    report = ValidationReport(collector=collector)
    for check in checks:
        report.add(check)
    return report
