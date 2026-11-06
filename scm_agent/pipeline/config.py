"""Central configuration — the single swap point for the model layer.

The whole point of this file is that NOTHING else in the codebase needs to know
whether we are talking to a fake model, a base OmniSQL served by vLLM, or a
LoRA fine-tuned OmniSQL. You change environment variables, not code.

    Stage 1 (today, no GPU):
        SCM_MODEL_PROVIDER=mock

    Stage 2 (base OmniSQL on a GPU):
        SCM_MODEL_PROVIDER=openai_compatible
        SCM_MODEL_NAME=seeklhy/OmniSQL-7B
        SCM_MODEL_BASE_URL=http://localhost:8000/v1

    Stage 3 (our fine-tuned adapter, same server):
        SCM_MODEL_PROVIDER=openai_compatible
        SCM_MODEL_NAME=seeklhy/OmniSQL-7B
        SCM_MODEL_BASE_URL=http://localhost:8000/v1
        SCM_LORA_ADAPTER=omnisql-multidialect-v1

vLLM can serve a LoRA adapter alongside the base weights and expose it as just
another model name on an OpenAI-compatible endpoint, which is why stage 2 -> 3
is a config change rather than a rewrite.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum


class ModelProvider(str, Enum):
    """Where completions come from."""

    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"


class Dialect(str, Enum):
    """SQL dialects the NL->SQL path can target.

    Deliberately scoped to SQL engines. The self-healing engine is
    PostgreSQL-only by design (VACUUM / REINDEX / pg_stat_* have no portable
    equivalent), so it does not consult this enum.
    """

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    SQLSERVER = "sqlserver"
    SQLITE = "sqlite"


@dataclass(frozen=True)
class DialectSpec:
    """Everything the prompt builder and safety layer need to know per dialect.

    Centralising these avoids the failure mode where dialect assumptions get
    hardcoded as string literals in three different modules and then drift.
    """

    prompt_name: str
    """Exact string for OmniSQL's `Database engine:` prompt line."""

    identifier_quotes: tuple[str, ...]
    """Characters used to quote identifiers, e.g. "users" vs [users]."""

    line_comment_tokens: tuple[str, ...]
    """Line-comment markers. Part of the SQL-injection surface, so safety.py
    strips these before scanning for write operations."""

    supports_returning: bool
    """Whether INSERT/UPDATE ... RETURNING exists. Postgres-family only."""

    sqlglot_name: str
    """The dialect name sqlglot parses with, e.g. "postgres" or "tsql"."""


DIALECT_SPECS: dict[Dialect, DialectSpec] = {
    Dialect.POSTGRESQL: DialectSpec(
        prompt_name="PostgreSQL",
        sqlglot_name="postgres",
        identifier_quotes=('"',),
        line_comment_tokens=("--",),
        supports_returning=True,
    ),
    Dialect.MYSQL: DialectSpec(
        prompt_name="MySQL",
        sqlglot_name="mysql",
        identifier_quotes=("`",),
        # MySQL additionally treats `#` as a line comment, which is exactly the
        # kind of dialect quirk an injection attempt would hide behind.
        line_comment_tokens=("--", "#"),
        supports_returning=False,
    ),
    Dialect.SQLSERVER: DialectSpec(
        prompt_name="SQL Server",
        sqlglot_name="tsql",
        identifier_quotes=("[", "]", '"'),
        line_comment_tokens=("--",),
        supports_returning=False,
    ),
    Dialect.SQLITE: DialectSpec(
        # OmniSQL's native training dialect — useful as a control when we
        # measure how much the fine-tune costs us on the dialect it started on.
        prompt_name="SQLite",
        sqlglot_name="sqlite",
        identifier_quotes=('"', "`", "[", "]"),
        line_comment_tokens=("--",),
        supports_returning=True,
    ),
}


@dataclass(frozen=True)
class ModelConfig:
    """Model layer settings. Read once at startup via `from_env`."""

    provider: ModelProvider
    model_name: str
    base_url: str | None
    api_key: str | None = field(repr=False)
    """Excluded from `repr`: a dataclass repr prints every field, so this object
    landing in a traceback, a log line or an f-string would print the API key.
    The value is unchanged and still readable as `config.model.api_key`."""
    lora_adapter: str | None
    temperature: float
    max_tokens: int

    @classmethod
    def from_env(cls) -> "ModelConfig":
        provider = ModelProvider(
            os.getenv("SCM_MODEL_PROVIDER", ModelProvider.MOCK.value).strip().lower()
        )
        return cls(
            provider=provider,
            model_name=os.getenv("SCM_MODEL_NAME", "seeklhy/OmniSQL-7B").strip(),
            base_url=_clean(os.getenv("SCM_MODEL_BASE_URL")),
            # vLLM ignores the key but the OpenAI client requires a non-empty one.
            api_key=_clean(os.getenv("SCM_MODEL_API_KEY")) or "EMPTY",
            lora_adapter=_clean(os.getenv("SCM_LORA_ADAPTER")),
            temperature=float(os.getenv("SCM_MODEL_TEMPERATURE", "0.0")),
            max_tokens=int(os.getenv("SCM_MODEL_MAX_TOKENS", "1024")),
        )

    @property
    def served_model(self) -> str:
        """The name to send as `model` in the request.

        When a LoRA adapter is loaded, vLLM addresses it by adapter name rather
        than by the base model name — so this is the whole stage 2 -> 3 swap.
        """
        return self.lora_adapter or self.model_name

    @property
    def stage(self) -> str:
        """Human-readable pipeline stage, surfaced on /health.

        Handy at the viva: it makes the mock/base/fine-tuned distinction
        impossible to fudge on stage.
        """
        if self.provider is ModelProvider.MOCK:
            return "stage-1-mock"
        if self.lora_adapter:
            return "stage-3-finetuned"
        return "stage-2-base"

    def validate(self) -> None:
        """Fail loudly at startup rather than mysteriously at request time."""
        if self.provider is ModelProvider.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError(
                "SCM_MODEL_BASE_URL is required when "
                "SCM_MODEL_PROVIDER=openai_compatible "
                "(e.g. http://localhost:8000/v1 for a local vLLM server)."
            )
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(f"temperature out of range: {self.temperature}")
        if self.max_tokens < 1:
            raise ValueError(f"max_tokens must be positive: {self.max_tokens}")


@dataclass(frozen=True)
class DatabaseConfig:
    """Target database the agent introspects and queries."""

    dsn: str = field(repr=False)
    """Excluded from `repr` — it carries a password, and a dataclass repr prints
    every field."""
    dialect: Dialect

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            # Points at THIS project's disposable compose instance on 5434.
            # The previous fallback used port 5432, which docker-compose.yml
            # documents as belonging to an unrelated project — so running
            # without SCM_DB_DSN set would connect to someone else's database
            # as user `postgres`.
            dsn=os.getenv(
                "SCM_DB_DSN", "postgresql://scm:scm@localhost:5434/scm_demo"
            ).strip(),
            dialect=Dialect(
                os.getenv("SCM_DIALECT", Dialect.POSTGRESQL.value).strip().lower()
            ),
        )

    @property
    def spec(self) -> DialectSpec:
        return DIALECT_SPECS[self.dialect]


@dataclass(frozen=True)
class AppConfig:
    model: ModelConfig
    database: DatabaseConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        config = cls(model=ModelConfig.from_env(), database=DatabaseConfig.from_env())
        config.model.validate()
        return config

    def summary(self) -> dict[str, object]:
        """Non-secret view of the config, safe to expose on /health.

        Deliberately omits the DSN — it carries a password — and reduces the
        model base URL to its origin. A URL may legitimately carry credentials
        in its userinfo component (`http://user:pass@host:8000/v1`), and
        publishing the raw value would hand them to every caller of /health.
        Scheme, host and port are enough to answer "which endpoint is this
        pointed at?", which is the only reason the field is there.
        """
        return {
            "stage": self.model.stage,
            "provider": self.model.provider.value,
            "served_model": self.model.served_model,
            "base_url": _safe_origin(self.model.base_url),
            "lora_adapter": self.model.lora_adapter,
            "dialect": self.database.dialect.value,
            "dialect_prompt_name": self.database.spec.prompt_name,
        }


def _safe_origin(url: str | None) -> str | None:
    """Reduce a URL to `scheme://host[:port]`, dropping userinfo and path.

    A base URL may legitimately carry credentials in its userinfo component
    (`http://user:pass@gpu-box:8000/v1`), and `/health` is unauthenticated, so
    republishing the raw value would hand those credentials to every caller.
    The origin answers "which endpoint is this pointed at?", which is the only
    reason the field is exposed at all.

    Anything unparseable becomes `"<redacted>"` rather than being echoed back:
    an unrecognised string is precisely the case where we cannot prove there is
    no secret in it, so it fails closed.
    """
    if url is None:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "<redacted>"
    if not parts.scheme or not parts.hostname:
        return "<redacted>"
    origin = f"{parts.scheme}://{parts.hostname}"
    return f"{origin}:{port}" if port else origin


def _clean(value: str | None) -> str | None:
    """Treat empty/whitespace env vars as unset — `export FOO=` is a common slip."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def get_config() -> AppConfig:
    """Entry point every other module should use."""
    return AppConfig.from_env()


if __name__ == "__main__":
    import json

    # Self-test: prove all three stages resolve from env vars alone, that every
    # dialect is registered, and that misconfiguration fails at startup.
    MANAGED = [
        "SCM_MODEL_PROVIDER",
        "SCM_MODEL_NAME",
        "SCM_MODEL_BASE_URL",
        "SCM_LORA_ADAPTER",
        "SCM_DIALECT",
        "SCM_DB_DSN",
    ]

    def apply(env: dict[str, str]) -> None:
        for key in MANAGED:
            os.environ.pop(key, None)
        os.environ.update(env)

    scenarios = [
        ("Stage 1 - mock (today, no GPU)", {"SCM_MODEL_PROVIDER": "mock"}),
        (
            "Stage 2 - base OmniSQL on vLLM",
            {
                "SCM_MODEL_PROVIDER": "openai_compatible",
                "SCM_MODEL_NAME": "seeklhy/OmniSQL-7B",
                "SCM_MODEL_BASE_URL": "http://localhost:8000/v1",
            },
        ),
        (
            "Stage 3 - fine-tuned LoRA adapter",
            {
                "SCM_MODEL_PROVIDER": "openai_compatible",
                "SCM_MODEL_NAME": "seeklhy/OmniSQL-7B",
                "SCM_MODEL_BASE_URL": "http://localhost:8000/v1",
                "SCM_LORA_ADAPTER": "omnisql-multidialect-v1",
            },
        ),
        (
            "Stage 2 + MySQL target (multi-dialect)",
            {
                "SCM_MODEL_PROVIDER": "openai_compatible",
                "SCM_MODEL_BASE_URL": "http://localhost:8000/v1",
                "SCM_DIALECT": "mysql",
                "SCM_DB_DSN": "mysql://root@localhost:3306/shop",
            },
        ),
    ]

    for title, env in scenarios:
        apply(env)
        print(f"\n{title}")
        print(json.dumps(get_config().summary(), indent=2))

    print("\nRegistered dialects:")
    for dialect, spec in DIALECT_SPECS.items():
        print(
            f"  {dialect.value:<12} prompt_name={spec.prompt_name!r:<14} "
            f"comments={spec.line_comment_tokens}"
        )

    print("\nCredential safety — summary() must not leak the DSN:")
    apply({"SCM_DB_DSN": "postgresql://admin:hunter2@db.giki.edu.pk:5432/prod"})
    leaked = "hunter2" in json.dumps(get_config().summary())
    print(f"  password present in /health payload: {leaked}")

    print("\nMisconfiguration is caught at startup:")
    apply({"SCM_MODEL_PROVIDER": "openai_compatible"})
    try:
        get_config()
        print("  FAILED: expected a ValueError for the missing base URL")
    except ValueError as exc:
        print(f"  ValueError raised as expected -> {exc}")

    for key in MANAGED:
        os.environ.pop(key, None)
