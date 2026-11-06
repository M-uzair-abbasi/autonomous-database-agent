"""Question -> `SQLCandidate`: step 4 of Flow A in CLAUDE.md, without the loop.

Reuses the parts of `scm_agent/pipeline` that already work and are tested:
schema introspection, the OmniSQL prompt template, the model client and SQL
extraction. What this adds is what the contracts need on top: a typed
`SQLCandidate`, the tables it reads, and a failure that names the stage that
failed.

The stage matters for evaluation. An `extract` failure is the model's fault and
counts against its accuracy. A `schema` or `model` failure is the
infrastructure's fault (database down, Ollama not running) and must not, or
the headline number measures the laptop instead of the model (GPT review I7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp

from contracts import SQLCandidate
from scm_agent.pipeline.config import DIALECT_SPECS, AppConfig, Dialect, get_config
from scm_agent.pipeline.model_client import ModelClient, get_model_client
from scm_agent.pipeline.nl_to_sql import (
    SQLExtractionError,
    build_prompt,
    extract_sql,
    get_schema_ddl,
)

Stage = Literal["input", "schema", "model", "extract"]


class GenerationError(Exception):
    """A question that did not become a candidate, and where it stopped."""

    def __init__(
        self, stage: Stage, message: str, prompt: str = "", raw_response: str = ""
    ) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.prompt = prompt
        """Kept so the failure can be replayed offline."""
        self.raw_response = raw_response

    @property
    def is_infrastructure(self) -> bool:
        """True when the model never got a fair chance to answer."""
        return self.stage in ("schema", "model")


@dataclass(frozen=True)
class Generation:
    candidate: SQLCandidate
    prompt: str
    raw_response: str
    """The model's full reply, reasoning prose included. This is the THOUGHT
    step the orchestrator logs."""


def generate_candidate(
    question: str,
    conn,
    config: AppConfig | None = None,
    client: ModelClient | None = None,
    schema: str | None = None,
) -> Generation:
    """Ask the model for one SQL query answering `question` against `conn`.

    Raises `GenerationError`; never returns a half-built candidate. Sampled row
    values are never put in the prompt: they are untrusted data (safety rule 9).
    """
    if config is None:
        config = get_config()
    dialect = config.database.dialect

    if not question or not question.strip():
        raise GenerationError("input", "empty question")

    try:
        ddl, _ = get_schema_ddl(conn, dialect, schema=schema)
    except Exception as exc:
        raise GenerationError(
            "schema", f"introspection failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        # Reading the catalog opens a transaction. Close it before the model
        # call, which can take minutes: an open one leaves the session idle in
        # transaction (the fault the healing engine alarms on) and holds back
        # VACUUM for the whole wait.
        _end_transaction(conn)
    if not ddl.strip():
        raise GenerationError("schema", "the database has no tables to query")

    prompt = build_prompt(question, ddl, dialect)

    try:
        if client is None:
            client = get_model_client(config.model)
        raw = client.generate(prompt)
    except Exception as exc:
        # Only the exception type goes in the message. SDK errors can quote the
        # endpoint URL, credentials included (GPT review I2); the chained
        # exception keeps the detail for local debugging.
        raise GenerationError(
            "model", f"model call failed: {type(exc).__name__}", prompt=prompt
        ) from exc

    try:
        sql = extract_sql(raw)
        candidate = SQLCandidate(
            question=question, sql=sql, tables_used=tables_in(sql, dialect)
        )
    except (SQLExtractionError, ValueError) as exc:
        raise GenerationError("extract", str(exc), prompt=prompt, raw_response=raw) from exc

    return Generation(candidate=candidate, prompt=prompt, raw_response=raw)


def _end_transaction(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass  # nothing was written; a failed rollback must not mask the real outcome


def tables_in(sql: str, dialect: Dialect) -> tuple[str, ...]:
    """Tables `sql` reads, sorted and de-duplicated. Empty if it does not parse.

    CTE names are not tables: `WITH recent AS (...) SELECT * FROM recent` reads
    whatever `recent` reads, not a table called `recent`. Rejecting SQL that
    does not parse is the validator's job, not this helper's.
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT_SPECS[dialect].sqlglot_name)
    except sqlglot.errors.SqlglotError:
        return ()
    if tree is None:
        return ()
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return tuple(
        sorted({t.name for t in tree.find_all(exp.Table) if t.name and t.name not in ctes})
    )
