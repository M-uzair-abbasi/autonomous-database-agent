"""The accuracy number is only worth reporting if every case is counted honestly.

Each test pins one way the old harness could misstate the headline figure
(GPT review C2, I7, I8, I10, M5): a broken reference shrinking the denominator,
a dead server counted as a wrong answer, an overflowing result scored as
correct, a malformed test set accepted, a credential written into a report.
"""

import json
import os
from pathlib import Path

import pytest

from modules.ask import evaluate as ev
from scm_agent.eval.eval_harness import open_evaluation_database
from scm_agent.pipeline.config import (
    AppConfig,
    DatabaseConfig,
    Dialect,
    ModelConfig,
    ModelProvider,
)

SCHEMA = Path(__file__).resolve().parents[1] / "scm_agent" / "db" / "schema.sql"


def config(dialect: Dialect = Dialect.SQLITE) -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            provider=ModelProvider.MOCK,
            model_name="mock",
            base_url=None,
            api_key=None,
            lora_adapter=None,
            temperature=0.0,
            max_tokens=1024,
        ),
        database=DatabaseConfig(dsn="sqlite://secret-marker", dialect=dialect),
    )


def case(**overrides) -> dict:
    fields = {
        "id": "c1",
        "difficulty": "easy",
        "question": "Which products are out of stock?",
        "expected_sql": "SELECT name FROM products WHERE in_stock = 0",
        "tags": ["filter"],
    }
    fields.update(overrides)
    return fields


def write_suite(tmp_path: Path, cases: list[dict], **top) -> Path:
    payload = {"dialect": "sqlite", "schema_file": str(SCHEMA), "cases": cases}
    payload.update(top)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def conn():
    fixture = open_evaluation_database()
    yield fixture.connection
    try:
        fixture.connection.close()
    except Exception:
        pass


class Reply:
    """A model that always gives the same answer, and counts how often it was asked."""

    def __init__(self, sql: str | None = None, text: str | None = None):
        self.text = text if text is not None else f"Reasoning.\n\n```sql\n{sql}\n```"
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        return self.text


class Unreachable:
    def generate(self, prompt: str) -> str:
        raise ConnectionError("model server is not running")


def run(tmp_path, conn, cases, client, **kwargs):
    suite = ev.load_suite(write_suite(tmp_path, cases))
    return ev.evaluate(suite, conn, config(), client=client, **kwargs)


class TestLoadSuite:
    def test_project_test_set_loads(self):
        suite = ev.load_suite()
        assert len(suite.cases) == 15
        assert suite.dialect is Dialect.POSTGRESQL
        assert len(suite.sha256) == 64

    @pytest.mark.parametrize(
        "cases, top",
        [
            ([case(), case()], {}),  # duplicate id
            ([{k: v for k, v in case().items() if k != "id"}], {}),  # missing id
            ([case(difficulty="trivial")], {}),
            ([case(tags=["filter", "filter"])], {}),  # would double-count in the tag report
            ([case(expected="SELECT 1")], {}),  # misspelled field
            ([case(question="  ")], {}),
            ([], {}),
            ([case()], {"dialect": "oracle"}),
            ([case()], {"schema_file": "no/such/schema.sql"}),
        ],
        ids=[
            "duplicate-id",
            "missing-id",
            "unknown-difficulty",
            "duplicate-tag",
            "unknown-field",
            "blank-question",
            "no-cases",
            "unknown-dialect",
            "missing-schema-file",
        ],
    )
    def test_malformed_suite_is_rejected(self, tmp_path, cases, top):
        with pytest.raises(ValueError):
            ev.load_suite(write_suite(tmp_path, cases, **top))


class TestIsOrdered:
    @pytest.mark.parametrize(
        "sql, expected",
        [
            ("SELECT name FROM products ORDER BY unit_price DESC LIMIT 3", True),
            ("SELECT * FROM (SELECT name FROM products ORDER BY name) AS t", False),
            ("SELECT name, RANK() OVER (ORDER BY unit_price) FROM products", False),
            ("SELECT $$ ORDER BY $$ AS label FROM products", False),
            (
                "WITH t AS (SELECT name FROM products ORDER BY name) SELECT name FROM t ORDER BY name",
                True,
            ),
        ],
        ids=["top-level", "subquery-only", "window-only", "dollar-quoted", "cte-and-outer"],
    )
    def test_only_the_outermost_order_by_counts(self, sql, expected):
        assert ev.is_ordered(sql, Dialect.POSTGRESQL) is expected


class TestEvaluate:
    def test_broken_reference_aborts_before_the_model_is_asked(self, tmp_path, conn):
        client = Reply("SELECT 1")
        cases = [case(), case(id="c2", expected_sql="SELECT no_such_column FROM products")]
        with pytest.raises(ev.PreflightError) as caught:
            run(tmp_path, conn, cases, client)
        assert "c2" in str(caught.value)
        assert client.calls == 0

    def test_suite_written_for_another_dialect_is_refused(self, tmp_path, conn):
        suite = ev.load_suite(write_suite(tmp_path, [case()], dialect="postgresql"))
        with pytest.raises(ValueError):
            ev.evaluate(suite, conn, config(Dialect.SQLITE), client=Reply("SELECT 1"))

    def test_correct_rows_in_any_order_match(self, tmp_path, conn):
        result = run(tmp_path, conn, [case()], Reply("SELECT name FROM products WHERE in_stock < 1"))
        assert result.results[0].outcome is ev.Outcome.MATCH
        assert result.accuracy == 1.0

    def test_wrong_order_fails_when_the_question_asks_for_order(self, tmp_path, conn):
        ordered = case(expected_sql="SELECT name FROM products ORDER BY unit_price DESC LIMIT 3")
        reply = Reply("SELECT name FROM products ORDER BY unit_price ASC LIMIT 3")
        result = run(tmp_path, conn, [ordered], reply)
        assert result.results[0].outcome is ev.Outcome.MISMATCH

    def test_reply_without_sql_counts_against_the_model(self, tmp_path, conn):
        result = run(tmp_path, conn, [case()], Reply(text="I do not know."))
        assert result.results[0].outcome is ev.Outcome.NO_SQL
        assert result.accuracy == 0.0

    def test_refused_query_counts_against_the_model(self, tmp_path, conn):
        result = run(tmp_path, conn, [case()], Reply("DROP TABLE products"))
        assert result.results[0].outcome is ev.Outcome.REJECTED
        assert result.accuracy == 0.0

    def test_result_past_the_row_cap_is_wrong_not_inconclusive(self, tmp_path, conn):
        # The reference fits under the cap (preflight guarantees it), so a
        # candidate that overflows the cap has more rows than the reference.
        counted = case(expected_sql="SELECT COUNT(*) FROM customers")
        result = run(tmp_path, conn, [counted], Reply("SELECT * FROM customers"), max_rows=3)
        assert result.results[0].outcome is ev.Outcome.MISMATCH
        assert result.accuracy == 0.0

    def test_unreachable_model_invalidates_the_run_instead_of_scoring_it(self, tmp_path, conn):
        result = run(tmp_path, conn, [case()], Unreachable())
        assert result.results[0].outcome is ev.Outcome.INFRA_ERROR
        assert result.accuracy is None
        assert not result.valid

    def test_database_dying_mid_run_is_infrastructure(self, tmp_path, conn):
        class KillsDatabase:
            def generate(self, prompt: str) -> str:
                conn.close()
                return "```sql\nSELECT name FROM products WHERE in_stock = 0\n```"

        result = run(tmp_path, conn, [case()], KillsDatabase())
        assert result.results[0].outcome is ev.Outcome.INFRA_ERROR
        assert not result.valid

    def test_breakdowns_count_each_case_once(self, tmp_path, conn):
        cases = [
            case(id="a", tags=["filter", "single-table"]),
            case(id="b", difficulty="hard", tags=["filter"]),
        ]
        result = run(tmp_path, conn, cases, Reply("SELECT name FROM products WHERE in_stock = 0"))
        assert result.breakdown("difficulty") == {"easy": (1, 1), "hard": (1, 1)}
        assert result.breakdown("tag") == {"filter": (2, 2), "single-table": (1, 1)}

    def test_report_records_provenance_without_credentials(self, tmp_path, conn):
        result = run(tmp_path, conn, [case()], Reply("SELECT name FROM products WHERE in_stock = 0"))
        report = result.to_dict()
        blob = json.dumps(report)

        assert "secret-marker" not in blob
        assert report["provenance"]["model"]["stage"] == "stage-1-mock"
        assert len(report["provenance"]["test_set"]["sha256"]) == 64
        assert len(report["provenance"]["schema"]["sha256"]) == 64
        assert "commit" in report["provenance"]["git"]
        assert report["latency_ms"]["p50"] is not None


LIVE_DSN = os.getenv("SCM_TEST_PG_DSN")
live = pytest.mark.skipif(not LIVE_DSN, reason="set SCM_TEST_PG_DSN to the disposable compose database")


def eval_dsn() -> str:
    from psycopg import conninfo

    return conninfo.make_conninfo(LIVE_DSN, dbname=ev.EVAL_DATABASE)


@live
def test_setup_refuses_any_database_but_the_eval_one():
    with pytest.raises(ValueError):
        ev.setup_eval_database(LIVE_DSN, SCHEMA)  # LIVE_DSN points at scm_demo


@live
def test_connection_guard_refuses_any_database_but_the_eval_one():
    with pytest.raises(ValueError):
        ev.open_eval_connection(LIVE_DSN)


@live
def test_full_project_test_set_runs_on_postgres():
    ev.setup_eval_database(eval_dsn(), SCHEMA)
    conn = ev.open_eval_connection(eval_dsn())
    try:
        result = ev.evaluate(ev.load_suite(), conn, config(Dialect.POSTGRESQL))
    finally:
        conn.close()
    # Every reference runs on PostgreSQL, so nothing is skipped this time.
    assert len(result.scored) == 15
    assert result.valid
