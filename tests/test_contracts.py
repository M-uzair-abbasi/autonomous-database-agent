"""The four shared contracts reject anything that is not the agreed shape.

A contract that quietly accepts a misspelled field or a missing value is not a
contract: the break only shows up as a wrong answer three modules later. Every
test here is a shape someone could plausibly send by mistake.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from contracts import AnomalyEvent, ExecutionResult, RemediationRecord, SQLCandidate


def make_candidate(**overrides) -> SQLCandidate:
    fields = {
        "question": "How many customers do we have?",
        "sql": "SELECT COUNT(*) FROM customers",
        "tables_used": ["customers"],
        "confidence": None,
    }
    fields.update(overrides)
    return SQLCandidate(**fields)


class TestSQLCandidate:
    def test_accepts_the_agreed_shape(self):
        candidate = make_candidate()
        assert candidate.tables_used == ("customers",)
        assert candidate.confidence is None

    @pytest.mark.parametrize("field", ["question", "sql"])
    def test_blank_text_is_rejected(self, field):
        with pytest.raises(ValidationError):
            make_candidate(**{field: "   "})

    @pytest.mark.parametrize("value", [-0.1, 1.5])
    def test_confidence_must_be_a_probability(self, value):
        with pytest.raises(ValidationError):
            make_candidate(confidence=value)

    def test_unknown_field_is_a_contract_break(self):
        with pytest.raises(ValidationError):
            make_candidate(confidance=0.9)

    def test_is_immutable(self):
        candidate = make_candidate()
        with pytest.raises(ValidationError):
            candidate.sql = "DROP TABLE customers"

    def test_round_trips_through_json(self):
        candidate = make_candidate(confidence=0.75)
        assert SQLCandidate.model_validate_json(candidate.model_dump_json()) == candidate


class TestExecutionResult:
    def test_success_carries_rows_and_no_reason(self):
        result = ExecutionResult(columns=["n"], rows=[[9]])
        assert result.rejected is False
        assert result.reason is None
        assert result.rows == ((9,),)

    def test_rejection_must_say_why(self):
        with pytest.raises(ValidationError):
            ExecutionResult(rejected=True)

    def test_rejection_carries_no_rows(self):
        with pytest.raises(ValidationError):
            ExecutionResult(rejected=True, reason="not read-only", columns=["n"], rows=[[1]])

    def test_reason_without_rejection_is_ambiguous(self):
        with pytest.raises(ValidationError):
            ExecutionResult(columns=["n"], rows=[[1]], reason="something happened")

    def test_every_row_matches_the_column_count(self):
        with pytest.raises(ValidationError):
            ExecutionResult(columns=["a", "b"], rows=[[1]])

    def test_truncation_is_reported(self):
        result = ExecutionResult(columns=["n"], rows=[[1], [2]], truncated=True)
        assert result.truncated is True

    def test_round_trips_through_json(self):
        result = ExecutionResult(
            columns=["name", "total"],
            rows=[["Keyboard", 179.98]],
            chart_spec={"type": "bar", "x": "name", "y": "total"},
        )
        assert ExecutionResult.model_validate_json(result.model_dump_json()) == result


class TestAnomalyEvent:
    def make(self, **overrides) -> AnomalyEvent:
        fields = {
            "timestamp": datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc),
            "severity": "warning",
            "metrics": {"idle_in_transaction": 3.0, "active_connections": 41.0},
            "deviating_features": ["idle_in_transaction"],
        }
        fields.update(overrides)
        return AnomalyEvent(**fields)

    def test_accepts_the_agreed_shape(self):
        assert self.make().severity == "warning"

    def test_timestamp_must_carry_a_timezone(self):
        # A naive timestamp is ambiguous by the server's offset, which is
        # exactly the error that makes fault windows fail to line up.
        with pytest.raises(ValidationError):
            self.make(timestamp=datetime(2026, 10, 18, 9, 30))

    def test_unknown_severity_is_rejected(self):
        with pytest.raises(ValidationError):
            self.make(severity="meh")

    def test_round_trips_through_json(self):
        event = self.make()
        assert AnomalyEvent.model_validate_json(event.model_dump_json()) == event


class TestRemediationRecord:
    def make(self, **overrides) -> RemediationRecord:
        fields = {
            "diagnosis": "dead tuples on orders after a bulk update",
            "action": 'VACUUM (SKIP_LOCKED TRUE, TRUNCATE FALSE) public."orders"',
            "predicted": {"dead_tuple_ratio": 0.02},
            "actual": None,
            "verified": False,
            "undo": "none - non-destructive: VACUUM only reclaims dead tuples",
        }
        fields.update(overrides)
        return RemediationRecord(**fields)

    def test_accepts_a_pre_action_record(self):
        assert self.make().actual is None

    def test_undo_must_be_written_before_the_do(self):
        # Safety rule 6: no remediation without its reverse already stored, or
        # an explicit statement that there is nothing to reverse.
        with pytest.raises(ValidationError):
            self.make(undo="")

    def test_cannot_be_verified_without_a_measurement(self):
        with pytest.raises(ValidationError):
            self.make(verified=True, actual=None)

    def test_verified_with_measurement_is_accepted(self):
        record = self.make(verified=True, actual={"dead_tuple_ratio": 0.01})
        assert record.verified is True

    def test_round_trips_through_json(self):
        record = self.make()
        assert RemediationRecord.model_validate_json(record.model_dump_json()) == record
