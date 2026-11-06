"""Question in, `SQLCandidate` out — and every way that can fail is named.

The failure stage matters as much as the SQL. Evaluation charges an
`extract` failure to the model, but a `model` or `schema` failure is the
infrastructure's fault and must never lower the model's accuracy figure.
"""

import os

import pytest

from contracts import SQLCandidate
from modules.ask.generate import GenerationError, generate_candidate, tables_in
from scm_agent.eval.eval_harness import open_evaluation_database
from scm_agent.pipeline.config import (
    AppConfig,
    DatabaseConfig,
    Dialect,
    ModelConfig,
    ModelProvider,
)


@pytest.fixture
def config() -> AppConfig:
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
        database=DatabaseConfig(dsn="sqlite://", dialect=Dialect.SQLITE),
    )


@pytest.fixture
def conn():
    fixture = open_evaluation_database()
    yield fixture.connection
    fixture.connection.close()


class FixedReply:
    def __init__(self, reply: str):
        self.reply = reply

    def generate(self, prompt: str) -> str:
        return self.reply


class Unreachable:
    def generate(self, prompt: str) -> str:
        raise ConnectionError("model server is not running")


def test_mock_model_produces_a_candidate(conn, config):
    generation = generate_candidate("How many customers do we have?", conn, config)

    assert isinstance(generation.candidate, SQLCandidate)
    assert generation.candidate.question == "How many customers do we have?"
    assert generation.candidate.tables_used == ("customers",)
    # Not estimated yet; a made-up number would be worse than none.
    assert generation.candidate.confidence is None
    assert "CREATE TABLE" in generation.prompt
    assert "```sql" in generation.raw_response


def test_blank_question_fails_before_any_work(conn, config):
    with pytest.raises(GenerationError) as caught:
        generate_candidate("   ", conn, config, client=Unreachable())
    assert caught.value.stage == "input"


def test_unreachable_model_is_an_infrastructure_failure(conn, config):
    with pytest.raises(GenerationError) as caught:
        generate_candidate("How many customers?", conn, config, client=Unreachable())
    assert caught.value.stage == "model"
    assert caught.value.is_infrastructure
    assert caught.value.prompt  # kept, so the failure can be replayed


def test_reply_without_sql_is_the_models_failure(conn, config):
    reply = "I am not sure which table holds that."
    with pytest.raises(GenerationError) as caught:
        generate_candidate("How many customers?", conn, config, client=FixedReply(reply))
    assert caught.value.stage == "extract"
    assert not caught.value.is_infrastructure
    assert caught.value.raw_response == reply


def test_tables_used_comes_from_the_sql_not_the_question(conn, config):
    reply = (
        "Join orders to customers.\n\n```sql\n"
        "SELECT c.full_name, COUNT(o.order_id) FROM customers c "
        "JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.full_name;\n```"
    )
    generation = generate_candidate("Orders per customer?", conn, config, client=FixedReply(reply))
    assert generation.candidate.tables_used == ("customers", "orders")


LIVE_DSN = os.getenv("SCM_TEST_PG_DSN")


@pytest.mark.skipif(not LIVE_DSN, reason="set SCM_TEST_PG_DSN to the disposable compose database")
def test_no_transaction_is_held_open_while_the_model_thinks(config):
    # A slow model (minutes on a shared GPU) would otherwise leave the session
    # idle in transaction: the exact fault the healing engine alarms on, and
    # one that holds back VACUUM for as long as the model takes.
    psycopg = pytest.importorskip("psycopg")
    pg_config = AppConfig(
        model=config.model,
        database=DatabaseConfig(dsn="unused", dialect=Dialect.POSTGRESQL),
    )
    seen = []

    class Watcher:
        def generate(self, prompt: str) -> str:
            seen.append(pg.info.transaction_status.name)
            return "```sql\nSELECT 1\n```"

    with psycopg.connect(LIVE_DSN) as pg:
        generate_candidate("How many customers?", pg, pg_config, client=Watcher())
    assert seen == ["IDLE"]


class TestTablesIn:
    def test_cte_names_are_not_tables(self):
        sql = (
            "WITH recent AS (SELECT * FROM orders WHERE order_date > '2024-01-01') "
            "SELECT * FROM recent JOIN customers USING (customer_id)"
        )
        assert tables_in(sql, Dialect.POSTGRESQL) == ("customers", "orders")

    def test_subquery_tables_are_included(self):
        sql = "SELECT name FROM products WHERE product_id NOT IN (SELECT product_id FROM order_items)"
        assert tables_in(sql, Dialect.POSTGRESQL) == ("order_items", "products")

    def test_unparseable_sql_yields_no_tables(self):
        # Rejecting the statement is the validator's job, not this helper's.
        assert tables_in("SELECT FROM WHERE (((", Dialect.POSTGRESQL) == ()
