"""The stand-in executor is safe enough to point at a real database.

It replaces Raneem's validator and executor until those exist, and the
evaluation runs model-written SQL through it, so it has to hold the two
guarantees CLAUDE.md puts below the prompt: a statement check, and a database
session that physically cannot write even if that check is wrong.
"""

import os

import pytest

from contracts import SQLCandidate
from modules.ask.stubs import execute as stub
from scm_agent.eval.eval_harness import open_evaluation_database
from scm_agent.pipeline.config import Dialect
from scm_agent.pipeline.safety import SafetyVerdict


def candidate(sql: str) -> SQLCandidate:
    return SQLCandidate(question="test", sql=sql, tables_used=(), confidence=None)


@pytest.fixture
def conn():
    fixture = open_evaluation_database()
    yield fixture.connection
    fixture.connection.close()


def count_customers(conn) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM customers")
    (value,) = cursor.fetchone()
    cursor.close()
    return value


def test_read_returns_rows(conn):
    result = stub.execute(candidate("SELECT COUNT(*) AS n FROM customers"), conn, Dialect.SQLITE)
    assert not result.rejected
    assert result.columns == ("n",)
    assert result.rows == ((9,),)


def test_write_is_rejected_before_execution(conn):
    result = stub.execute(candidate("DROP TABLE customers"), conn, Dialect.SQLITE)
    assert result.rejected
    assert result.rows == ()
    assert count_customers(conn) == 9


def test_row_cap_marks_the_result_truncated(conn):
    result = stub.execute(candidate("SELECT * FROM customers"), conn, Dialect.SQLITE, max_rows=2)
    assert len(result.rows) == 2
    assert result.truncated


def test_database_refuses_a_write_the_statement_check_missed(conn, monkeypatch):
    # Simulate a hole in the statement check. The session itself must still
    # refuse, which is the whole point of having a second layer.
    def allow_everything(sql, dialect):
        return SafetyVerdict(allowed=True, reason="forced", normalized_sql=sql)

    monkeypatch.setattr(stub, "check_sql", allow_everything)
    sql = (
        "INSERT INTO customers (full_name, email, signup_date) "
        "VALUES ('x', 'x@example.com', '2024-01-01')"
    )
    result = stub.execute(candidate(sql), conn, Dialect.SQLITE)
    assert result.rejected
    assert count_customers(conn) == 9


def test_unsupported_dialect_is_refused_not_guessed(conn):
    result = stub.execute(candidate("SELECT 1"), conn, Dialect.MYSQL)
    assert result.rejected


LIVE_DSN = os.getenv("SCM_TEST_PG_DSN")
live = pytest.mark.skipif(not LIVE_DSN, reason="set SCM_TEST_PG_DSN to the disposable compose database")


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(LIVE_DSN)
    yield connection
    connection.close()


@live
def test_postgres_read_returns_rows(pg):
    result = stub.execute(candidate("SELECT COUNT(*) AS n FROM customers"), pg, Dialect.POSTGRESQL)
    assert not result.rejected
    assert result.rows == ((9,),)


@live
def test_postgres_blocks_a_write_disguised_as_a_select(pg):
    # `SELECT nextval(...)` passes the lexical check (GPT review I1) but
    # advances a sequence. The read-only transaction has to stop it.
    sql = "SELECT nextval(pg_get_serial_sequence('customers', 'customer_id'))"
    result = stub.execute(candidate(sql), pg, Dialect.POSTGRESQL)
    assert result.rejected
    assert "read-only" in result.reason


@live
def test_postgres_statement_timeout_stops_a_slow_query(pg):
    # Plain SQL rather than pg_sleep(), which the statement check blocks
    # before the database ever sees it. Counting to 10^8 takes seconds.
    slow = (
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 100000000) "
        "SELECT COUNT(*) FROM t"
    )
    result = stub.execute(candidate(slow), pg, Dialect.POSTGRESQL, statement_timeout_ms=200)
    assert result.rejected
    assert "timeout" in result.reason


@live
def test_postgres_connection_is_not_left_idle_in_transaction(pg):
    stub.execute(candidate("SELECT 1"), pg, Dialect.POSTGRESQL)
    # psycopg reports IDLE (0) when no transaction is open.
    assert pg.info.transaction_status == 0
