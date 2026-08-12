"""Behavioral tests for healing runtime configuration."""

from __future__ import annotations

from datetime import timedelta
import os
import unittest
from unittest.mock import patch

from scm_agent.healing.config import HealingConfig, RulePolicy
from scm_agent.healing.models import Mode


class RulePolicyTests(unittest.TestCase):
    def test_defaults_are_conservative_literal_values(self) -> None:
        policy = RulePolicy()

        self.assertEqual(policy.dead_tuple_ratio, 0.20)
        self.assertEqual(policy.minimum_table_rows, 10_000)
        self.assertEqual(policy.minimum_dead_tuples, 5_000)
        self.assertEqual(policy.idle_transaction_warning, timedelta(minutes=5))
        self.assertEqual(policy.idle_transaction_action, timedelta(minutes=15))
        self.assertEqual(policy.long_query, timedelta(minutes=5))
        self.assertEqual(policy.connection_warning_ratio, 0.80)
        self.assertEqual(policy.connection_critical_ratio, 0.90)
        self.assertEqual(policy.unused_index_minimum_bytes, 104_857_600)
        self.assertEqual(policy.unused_index_maximum_scans, 0)
        self.assertEqual(policy.minimum_statistics_age, timedelta(days=7))

    def test_rejects_overlapping_connection_bands(self) -> None:
        with self.assertRaisesRegex(ValueError, "connection"):
            RulePolicy(connection_warning_ratio=0.95, connection_critical_ratio=0.90)

    def test_rejects_non_positive_thresholds_and_inverted_idle_windows(self) -> None:
        with self.assertRaisesRegex(ValueError, "dead_tuple_ratio"):
            RulePolicy(dead_tuple_ratio=0)
        with self.assertRaisesRegex(ValueError, "minimum_table_rows"):
            RulePolicy(minimum_table_rows=-1)
        with self.assertRaisesRegex(ValueError, "idle_transaction_action"):
            RulePolicy(
                idle_transaction_warning=timedelta(minutes=15),
                idle_transaction_action=timedelta(minutes=5),
            )


class HealingConfigFromEnvironmentTests(unittest.TestCase):
    def test_from_env_defaults_to_observe_and_safe_actions_off(self) -> None:
        config = HealingConfig.from_env({})

        self.assertIs(config.mode, Mode.OBSERVE)
        self.assertFalse(config.allow_terminate_idle_transaction)
        self.assertFalse(config.allow_manual_reindex)
        self.assertEqual(config.database_dsn, "postgresql://scm:scm@localhost:5434/scm_demo")
        self.assertEqual(config.poll_interval, timedelta(seconds=30))
        self.assertEqual(config.vacuum_cooldown, timedelta(hours=6))
        self.assertEqual(config.maximum_actions_per_cycle, 1)
        self.assertEqual(config.required_consecutive_detections, 2)

    def test_parses_strict_boolean_spellings(self) -> None:
        enabled = HealingConfig.from_env(
            {
                "SCM_HEALING_ALLOW_TERMINATE_IDLE_TRANSACTION": "yes",
                "SCM_HEALING_ALLOW_MANUAL_REINDEX": "on",
            }
        )
        disabled = HealingConfig.from_env(
            {
                "SCM_HEALING_ALLOW_TERMINATE_IDLE_TRANSACTION": "0",
                "SCM_HEALING_ALLOW_MANUAL_REINDEX": "false",
            }
        )

        self.assertTrue(enabled.allow_terminate_idle_transaction)
        self.assertTrue(enabled.allow_manual_reindex)
        self.assertFalse(disabled.allow_terminate_idle_transaction)
        self.assertFalse(disabled.allow_manual_reindex)
        with self.assertRaisesRegex(ValueError, "boolean"):
            HealingConfig.from_env({"SCM_HEALING_ALLOW_MANUAL_REINDEX": "maybe"})

    def test_rejects_invalid_mode_and_runtime_ranges(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode"):
            HealingConfig.from_env({"SCM_HEALING_MODE": "danger"})
        with self.assertRaisesRegex(ValueError, "poll"):
            HealingConfig.from_env({"SCM_HEALING_POLL_SECONDS": "0"})
        with self.assertRaisesRegex(ValueError, "audit"):
            HealingConfig.from_env({"SCM_HEALING_AUDIT_PATH": ""})
        with self.assertRaisesRegex(ValueError, "actions"):
            HealingConfig.from_env({"SCM_HEALING_MAX_ACTIONS_PER_CYCLE": "0"})
        with self.assertRaisesRegex(ValueError, "consecutive"):
            HealingConfig.from_env({"SCM_HEALING_REQUIRED_CONSECUTIVE": "0"})
        with self.assertRaisesRegex(ValueError, "vacuum"):
            HealingConfig.from_env({"SCM_HEALING_VACUUM_COOLDOWN_SECONDS": "60"})
        with self.assertRaisesRegex(ValueError, "timeout"):
            HealingConfig.from_env({"SCM_HEALING_STATEMENT_TIMEOUT_SECONDS": "0"})

    def test_v1_rejects_weakened_detection_and_action_safety_limits(self) -> None:
        for values, message in (
            ({"maximum_actions_per_cycle": 0}, "exactly one"),
            ({"maximum_actions_per_cycle": 2}, "exactly one"),
            ({"required_consecutive_detections": 1}, "at least two"),
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, message):
                    HealingConfig(**values)

        for environ, message in (
            ({"SCM_HEALING_MAX_ACTIONS_PER_CYCLE": "2"}, "exactly one"),
            ({"SCM_HEALING_REQUIRED_CONSECUTIVE": "1"}, "at least two"),
        ):
            with self.subTest(environ=environ):
                with self.assertRaisesRegex(ValueError, message):
                    HealingConfig.from_env(environ)

    def test_safe_summary_omits_database_dsn_and_password(self) -> None:
        config = HealingConfig.from_env(
            {"SCM_DB_DSN": "postgresql://operator:top-secret@db.example/healing"}
        )

        summary = config.safe_summary()

        self.assertNotIn("database_dsn", summary)
        self.assertNotIn("top-secret", repr(summary))
        self.assertEqual(summary["mode"], "observe")

    def test_secret_dsn_is_absent_from_config_repr_and_safe_summary(self) -> None:
        config = HealingConfig.from_env(
            {"SCM_DB_DSN": "postgresql://operator:repr-regression-secret@db.example/healing"}
        )

        self.assertNotIn("repr-regression-secret", repr(config))
        self.assertNotIn("repr-regression-secret", repr(config.safe_summary()))

    def test_from_env_without_mapping_reads_process_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SCM_HEALING_MODE": "dry_run",
                "SCM_DB_DSN": "postgresql://scm:environment-secret@localhost:5434/scm_demo",
            },
            clear=True,
        ):
            config = HealingConfig.from_env()

        self.assertIs(config.mode, Mode.DRY_RUN)
        self.assertEqual(
            config.database_dsn,
            "postgresql://scm:environment-secret@localhost:5434/scm_demo",
        )


if __name__ == "__main__":
    unittest.main()
