"""The four types that cross an ownership boundary. See CLAUDE.md section 7.

    SQLCandidate       ask     -> execute   (Uzair  -> Raneem)
    ExecutionResult    execute -> ask       (Raneem -> Uzair)
    AnomalyEvent       watch   -> fix       (Warisha -> Yawar)
    RemediationRecord  fix     -> dashboard (Yawar  -> Yawar)

DRAFT until all four owners agree in the group chat. Field names follow
CLAUDE.md exactly. Additions or choices beyond CLAUDE.md are marked
"Beyond CLAUDE.md" so they are easy to find and argue about. After agreement
these are frozen: changing one means two people rewrite code, so a change goes
through the group first.

Every model is immutable and forbids unknown fields. A misspelled field must
fail loudly at the boundary, not turn up as a wrong answer three modules later.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

CONTRACTS_VERSION = "0.1-draft"

NonBlankStr = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SQLCandidate(_Contract):
    """One proposed query for one question. Produced by `ask`, judged by `execute`."""

    question: NonBlankStr
    sql: NonBlankStr
    tables_used: tuple[str, ...] = ()
    """Tables the SQL reads, parsed from the SQL itself, never guessed from the
    question. CTE names are not tables."""

    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    """Probability the SQL answers the question. Beyond CLAUDE.md: `None` is
    allowed and means "not estimated". There is no calibrated estimator yet,
    and an invented number is worse than an honest blank."""


class ExecutionResult(_Contract):
    """What happened to a candidate: rows, or a rejection with a reason.

    One shape rather than two so the boundary stays a single type. The
    validator below keeps the two cases from mixing.
    """

    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    chart_spec: dict[str, Any] | None = None
    rejected: bool = False
    reason: str | None = None
    """Why the candidate was rejected. Sent back to the model on retry, so it
    should name the problem, not just say "error"."""

    truncated: bool = False
    """Beyond CLAUDE.md: True when the row cap cut the result short. Needed so
    evaluation never scores the first N rows of a longer result as a correct
    answer (GPT review C2)."""

    @model_validator(mode="after")
    def _one_case_only(self) -> "ExecutionResult":
        if self.rejected:
            if not (self.reason and self.reason.strip()):
                raise ValueError("a rejection must say why")
            if self.rows or self.columns or self.chart_spec is not None:
                raise ValueError("a rejection carries no result")
        elif self.reason is not None:
            raise ValueError("reason is only for rejections")
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ValueError(f"row has {len(row)} values for {width} columns")
        return self


class AnomalyEvent(_Contract):
    """Something in the telemetry looks wrong. Produced by `watch`, handled by `fix`."""

    timestamp: AwareDatetime
    """Must carry a timezone. Fault start/end times are the ground-truth
    labels, and a naive timestamp shifts them by the server's UTC offset."""

    severity: Literal["warning", "critical"]
    """The same two levels the existing healing rules use."""

    metrics: dict[str, float]
    deviating_features: tuple[str, ...] = ()


class RemediationRecord(_Contract):
    """One repair attempt, start to finish. Produced by `fix`, shown on the dashboard."""

    diagnosis: NonBlankStr
    action: NonBlankStr
    predicted: dict[str, float]
    """Estimated value of each target metric after the fix, recorded before acting."""

    actual: dict[str, float] | None = None
    """Measured after the settling period. `None` until measured."""

    verified: bool = False
    undo: NonBlankStr
    """The reverse operation, or an explicit "none - non-destructive: <why>".
    Required because safety rule 6 says no remediation runs without it."""

    @model_validator(mode="after")
    def _verified_means_measured(self) -> "RemediationRecord":
        if self.verified and self.actual is None:
            raise ValueError("cannot be verified without a measurement")
        return self


__all__ = [
    "CONTRACTS_VERSION",
    "AnomalyEvent",
    "ExecutionResult",
    "RemediationRecord",
    "SQLCandidate",
]
