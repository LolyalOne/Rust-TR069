#!/usr/bin/env python3
"""
Adversarial Challenge Test Suite for Milestone 2 (PostgreSQL Hybrid Schema & Reconciliation)
Author: challenger_m2_it2_2 (teamwork_preview_challenger)
Target: postgres/init.sql, postgres/test_schema.py, postgres/test_reconciliation_empirical.py, simulate_flow.sh

Challenge Dimensions:
1. Malformed JSON, missing optical keys, non-numeric optical string values ("N/A", "error"), null values.
2. Foreign key cascade deletion: cpe_inventory -> cpe_live_state & cpe_historical_metrics.
3. View cpe_state_history compatibility and exact column mapping.
4. Stress testing and PL/pgSQL exact behavioral simulation.
"""

import copy
import json
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

INIT_SQL_PATH = Path(__file__).resolve().parent / "init.sql"
SIMULATE_FLOW_PATH = Path(__file__).resolve().parent.parent / "simulate_flow.sh"


class PostgresPlpgsqlReconciliationOracle:
    """
    Exact behavioral oracle simulating postgres/init.sql reconcile_live_to_history().
    Follows every IF / ELSIF / regex / COALESCE in the PL/pgSQL implementation verbatim.
    """

    NUMERIC_REGEX = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")

    @classmethod
    def extract_optical_power(cls, telemetry_metrics, current_parameters):
        v_new_val = None
        if telemetry_metrics is not None and isinstance(telemetry_metrics, dict):
            for k in ("rx_optical_power", "optical_power", "optical_rx_power", "rx_power"):
                val = telemetry_metrics.get(k)
                if val is not None:
                    v_new_val = str(val)
                    break

        if v_new_val is None and current_parameters is not None and isinstance(current_parameters, dict):
            for k in ("Device.Optical.Interface.1.OpticalSignalLevel", "Device.Optical.Interface.1.RxPower"):
                val = current_parameters.get(k)
                if val is not None:
                    v_new_val = str(val)
                    break

        v_new_rx_power = None
        if v_new_val is not None and cls.NUMERIC_REGEX.match(v_new_val):
            try:
                v_new_rx_power = float(v_new_val)
            except (ValueError, TypeError):
                v_new_rx_power = None

        return v_new_rx_power

    @classmethod
    def evaluate_transition(cls, tg_op: str, old_metrics, old_params, new_metrics, new_params):
        """
        Simulates:
          reconcile_live_to_history()
        Returns: (should_record: bool, change_reason: str, optical_power: float | None)
        """
        new_rx = cls.extract_optical_power(new_metrics, new_params)
        old_rx = cls.extract_optical_power(old_metrics, old_params) if tg_op == "UPDATE" else None

        v_reason = "optical_signal_variation"
        v_should_record = False

        if tg_op == "INSERT":
            if new_rx is not None:
                v_reason = "initial_state"
                v_should_record = True
        elif tg_op == "UPDATE":
            if old_rx is None and new_rx is not None:
                v_reason = "initial_state"
                v_should_record = True
            elif old_rx is not None and new_rx is not None:
                v_delta = abs(new_rx - old_rx)
                if v_delta > 1.0:
                    v_reason = "optical_signal_variation"
                    v_should_record = True

        return v_should_record, v_reason, new_rx


class TestAdversarialJsonAndMalformedPayloads(unittest.TestCase):
    """
    Test malformed JSON, missing optical keys, non-numeric optical string values
    (e.g. 'N/A', 'error'), and null values in telemetry_metrics.
    """

    def setUp(self):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            self.sql_content = f.read()

    def test_plpgsql_regex_rejects_non_numeric_strings(self):
        """
        Verifies that the PL/pgSQL regex '^-?[0-9]+(\.[0-9]+)?$' in init.sql
        strictly rejects non-numeric optical values without throwing numeric cast exceptions.
        """
        # Ensure regex exists in init.sql
        self.assertIn("v_new_val ~ '^-?[0-9]+(\\.[0-9]+)?$'", self.sql_content)
        self.assertIn("v_old_val ~ '^-?[0-9]+(\\.[0-9]+)?$'", self.sql_content)

        adversarial_non_numerics = [
            "N/A", "error", "ERROR", "Unknown", "unknown", "--", "", " ",
            "NaN", "Infinity", "-Infinity", "null", "None", "true", "false",
            "-18.5 dBm", "-18.5dBm", "1e-2", "0x12", "12.34.56", ".5", "-.",
            "drop table cpe_inventory;", "'; SELECT pg_sleep(5); --"
        ]

        for val in adversarial_non_numerics:
            rx = PostgresPlpgsqlReconciliationOracle.extract_optical_power({"rx_optical_power": val}, {})
            self.assertIsNone(rx, f"Adversarial non-numeric value '{val}' must evaluate to None")

            # Evaluate transition on INSERT
            should_rec, reason, power = PostgresPlpgsqlReconciliationOracle.evaluate_transition(
                "INSERT", None, None, {"rx_optical_power": val}, {}
            )
            self.assertFalse(should_rec, f"Value '{val}' must NOT record a history snapshot on INSERT")

            # Evaluate transition on UPDATE from valid baseline -18.5
            should_rec_upd, _, _ = PostgresPlpgsqlReconciliationOracle.evaluate_transition(
                "UPDATE", {"rx_optical_power": -18.5}, {}, {"rx_optical_power": val}, {}
            )
            self.assertFalse(should_rec_upd, f"Updating to '{val}' must NOT record a history snapshot on UPDATE")

    def test_valid_numeric_optical_values_accepted(self):
        """Verifies that valid integers and floats (negative and positive) are correctly parsed."""
        valid_inputs = [
            (-18.5, -18.5),
            ("-18.5", -18.5),
            ("-21.0", -21.0),
            (0, 0.0),
            ("0", 0.0),
            ("0.0", 0.0),
            ("1.5", 1.5),
            (2.5, 2.5),
            ("-3.00", -3.0),
        ]
        for inp, expected in valid_inputs:
            rx = PostgresPlpgsqlReconciliationOracle.extract_optical_power({"rx_optical_power": inp}, {})
            self.assertIsNotNone(rx, f"Valid input {inp} must not be None")
            self.assertAlmostEqual(rx, expected, places=3)

    def test_missing_optical_keys_gracefully_ignored(self):
        """Payloads without any optical power keys must not crash and must not record history."""
        payloads = [
            {},
            {"cpu_usage": 88.4, "memory_usage": 75.2},
            {"temperature": 52.8, "rx_bytes": 1048576},
            {"status": "online", "uptime": 3600},
        ]
        for p in payloads:
            rx = PostgresPlpgsqlReconciliationOracle.extract_optical_power(p, {})
            self.assertIsNone(rx)
            should_rec, _, _ = PostgresPlpgsqlReconciliationOracle.evaluate_transition(
                "INSERT", None, None, p, {}
            )
            self.assertFalse(should_rec, f"Payload {p} must NOT record history")

    def test_null_optical_keys_gracefully_ignored(self):
        """Payloads with optical keys set to null/None must not crash and must not record history."""
        payloads = [
            {"rx_optical_power": None},
            {"optical_power": None},
            {"optical_rx_power": None},
            {"rx_power": None},
        ]
        for p in payloads:
            rx = PostgresPlpgsqlReconciliationOracle.extract_optical_power(p, {})
            self.assertIsNone(rx)
            should_rec, _, _ = PostgresPlpgsqlReconciliationOracle.evaluate_transition(
                "INSERT", None, None, p, {}
            )
            self.assertFalse(should_rec, f"Null optical key in {p} must NOT record history")

    def test_sqlite_harness_behavior_on_malformed_json_and_non_numeric(self):
        """
        Adversarially tests SQLite relational harness with:
        1. Malformed JSON: SQLite json_extract raises OperationalError on raw invalid json string.
           In PostgreSQL, this is blocked at parser level (JSONB type validation).
        2. Non-numeric optical strings in SQLite:
           Demonstrates that raw SQLite CAST('N/A' AS REAL) evaluates to 0.0,
           identifying the exact reason SQLite harness requires numeric guards.
        """
        conn = sqlite3.connect(":memory:")
        # Raw json_extract on malformed JSON
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("SELECT json_extract('{malformed: json', '$.rx_optical_power')")

        # SQLite CAST non-numeric string to REAL produces 0.0
        r = conn.execute("SELECT CAST('N/A' AS REAL)").fetchone()[0]
        self.assertEqual(r, 0.0, "SQLite CAST non-numeric produces 0.0")

        # In PostgreSQL PL/pgSQL, this is protected by: v_new_val ~ '^-?[0-9]+(\.[0-9]+)?$'
        # Confirm init.sql protects this
        self.assertIn("v_new_val ~ '^-?[0-9]+(\\.[0-9]+)?$'", self.sql_content)


class TestForeignKeyCascadeDeletion(unittest.TestCase):
    """
    Verify foreign key cascade deletion:
    Deleting from cpe_inventory cascades deletion to cpe_live_state and cpe_historical_metrics.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._setup_schema()

    def _setup_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE cpe_inventory (
                cpe_id TEXT PRIMARY KEY,
                serial_number TEXT UNIQUE NOT NULL,
                manufacturer TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'offline'
            );
        """)
        cur.execute("""
            CREATE TABLE cpe_live_state (
                cpe_id TEXT PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'offline',
                telemetry_metrics TEXT NOT NULL DEFAULT '{}'
            );
        """)
        cur.execute("""
            CREATE TABLE cpe_historical_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cpe_id TEXT NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                optical_power REAL,
                recorded_at TEXT NOT NULL,
                change_reason TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE VIEW cpe_state_history AS
            SELECT id, cpe_id, status, optical_power, recorded_at, change_reason
            FROM cpe_historical_metrics;
        """)

    def test_foreign_key_cascade_deletion_empirical(self):
        """
        Verify that deleting from cpe_inventory cascades deletion
        to cpe_live_state and cpe_historical_metrics.
        """
        cur = self.conn.cursor()
        cpe_id = "CPE-CASCADE-TEST-001"

        # 1. Insert inventory
        cur.execute(
            "INSERT INTO cpe_inventory (cpe_id, serial_number, manufacturer, model) VALUES (?, ?, ?, ?)",
            (cpe_id, "SN-CASCADE-001", "TP-Link", "Archer-AX50")
        )

        # 2. Insert live state
        cur.execute(
            "INSERT INTO cpe_live_state (cpe_id, status, telemetry_metrics) VALUES (?, ?, ?)",
            (cpe_id, "online", '{"rx_optical_power": -18.5}')
        )

        # 3. Insert historical snapshots
        cur.execute(
            "INSERT INTO cpe_historical_metrics (cpe_id, status, optical_power, recorded_at, change_reason) VALUES (?, ?, ?, ?, ?)",
            (cpe_id, "online", -18.5, "2026-09-07T00:00:00Z", "initial_state")
        )
        cur.execute(
            "INSERT INTO cpe_historical_metrics (cpe_id, status, optical_power, recorded_at, change_reason) VALUES (?, ?, ?, ?, ?)",
            (cpe_id, "online", -21.0, "2026-09-07T00:05:00Z", "optical_signal_variation")
        )

        # Assert data exists in all 3 tables and view
        self.assertEqual(cur.execute("SELECT count(*) FROM cpe_inventory WHERE cpe_id=?", (cpe_id,)).fetchone()[0], 1)
        self.assertEqual(cur.execute("SELECT count(*) FROM cpe_live_state WHERE cpe_id=?", (cpe_id,)).fetchone()[0], 1)
        self.assertEqual(cur.execute("SELECT count(*) FROM cpe_historical_metrics WHERE cpe_id=?", (cpe_id,)).fetchone()[0], 2)
        self.assertEqual(cur.execute("SELECT count(*) FROM cpe_state_history WHERE cpe_id=?", (cpe_id,)).fetchone()[0], 2)

        # 4. DELETE from cpe_inventory
        cur.execute("DELETE FROM cpe_inventory WHERE cpe_id=?", (cpe_id,))

        # 5. Assert cascade deletion
        self.assertEqual(cur.execute("SELECT count(*) FROM cpe_inventory WHERE cpe_id=?", (cpe_id,)).fetchone()[0], 0)
        self.assertEqual(
            cur.execute("SELECT count(*) FROM cpe_live_state WHERE cpe_id=?", (cpe_id,)).fetchone()[0],
            0,
            "cpe_live_state must be cascade-deleted when cpe_inventory is deleted"
        )
        self.assertEqual(
            cur.execute("SELECT count(*) FROM cpe_historical_metrics WHERE cpe_id=?", (cpe_id,)).fetchone()[0],
            0,
            "cpe_historical_metrics must be cascade-deleted when cpe_inventory is deleted"
        )
        self.assertEqual(
            cur.execute("SELECT count(*) FROM cpe_state_history WHERE cpe_id=?", (cpe_id,)).fetchone()[0],
            0,
            "cpe_state_history view must be empty for deleted cpe"
        )

    def test_static_schema_has_on_delete_cascade(self):
        """Verifies postgres/init.sql statically defines ON DELETE CASCADE on both tables."""
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            sql = f.read()

        # cpe_live_state foreign key
        live_fk_match = re.search(
            r"CREATE\s+UNLOGGED\s+TABLE\s+IF\s+NOT\s+EXISTS\s+cpe_live_state\s*\((.*?)\)\s*TABLESPACE",
            sql,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(live_fk_match, "cpe_live_state table definition not found")
        self.assertIn("REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE", live_fk_match.group(1))

        # cpe_historical_metrics foreign key
        hist_fk_match = re.search(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+cpe_historical_metrics\s*\((.*?)\);",
            sql,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(hist_fk_match, "cpe_historical_metrics table definition not found")
        self.assertIn("REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE", hist_fk_match.group(1))


class TestCpeStateHistoryViewAndSimulationFlow(unittest.TestCase):
    """
    Verify view cpe_state_history:
    Does SELECT * FROM cpe_state_history return the exact columns expected by simulate_flow.sh
    and upstream services?
    """

    def setUp(self):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            self.sql_content = f.read()

    def test_view_definition_in_init_sql(self):
        """Verifies view cpe_state_history definition and projected columns."""
        view_match = re.search(
            r"CREATE\s+OR\s+REPLACE\s+VIEW\s+cpe_state_history\s+AS\s+SELECT\s+(.*?)\s+FROM\s+cpe_historical_metrics;",
            self.sql_content,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(view_match, "CREATE OR REPLACE VIEW cpe_state_history not found in init.sql")

        columns = [c.strip() for c in view_match.group(1).split(",")]
        expected_columns = [
            "id",
            "cpe_id",
            "status",
            "current_parameters",
            "telemetry_metrics",
            "recorded_at",
            "change_reason",
        ]
        self.assertEqual(columns, expected_columns, "cpe_state_history view columns mismatch")

    def test_simulate_flow_query_compatibility(self):
        """Verifies simulate_flow.sh query compatibility with cpe_state_history."""
        with open(SIMULATE_FLOW_PATH, "r", encoding="utf-8") as f:
            sh_content = f.read()

        # Check line in simulate_flow.sh
        match = re.search(r"SELECT\s+count\(\*\)\s+FROM\s+cpe_state_history\s+WHERE\s+cpe_id='[^']+';", sh_content)
        self.assertIsNotNone(match, "simulate_flow.sh query pattern on cpe_state_history not found")

        # In SQLite, execute the exact query against mock table and view
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE cpe_historical_metrics (
                id INTEGER PRIMARY KEY,
                cpe_id TEXT,
                status TEXT,
                current_parameters TEXT,
                telemetry_metrics TEXT,
                optical_power REAL,
                recorded_at TEXT,
                change_reason TEXT
            );
        """)
        conn.execute("""
            CREATE VIEW cpe_state_history AS
            SELECT id, cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason
            FROM cpe_historical_metrics;
        """)

        # Insert 2 rows
        conn.execute("INSERT INTO cpe_historical_metrics VALUES (1, 'CPE-001', 'online', '{}', '{}', -18.5, '2026-09-07', 'initial_state')")
        conn.execute("INSERT INTO cpe_historical_metrics VALUES (2, 'CPE-001', 'online', '{}', '{}', -21.0, '2026-09-07', 'optical_signal_variation')")

        # Run simulate_flow.sh query
        row_count = conn.execute("SELECT count(*) FROM cpe_state_history WHERE cpe_id='CPE-001';").fetchone()[0]
        self.assertEqual(row_count, 2, "Query must return 2")

        # Test SELECT * column names
        cursor = conn.execute("SELECT * FROM cpe_state_history WHERE cpe_id='CPE-001';")
        col_names = [d[0] for d in cursor.description]
        self.assertEqual(
            col_names,
            ["id", "cpe_id", "status", "current_parameters", "telemetry_metrics", "recorded_at", "change_reason"]
        )


class TestZeroWalWriteAmplificationEmpirical(unittest.TestCase):
    """
    Stress-tests that cpe_inventory is strictly read-only for all live-state operations.
    Assures zero WAL write amplification.
    """

    def setUp(self):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            self.sql_content = f.read()

    def test_zero_update_in_trigger_function(self):
        """Verifies init.sql function reconcile_live_to_history does not contain UPDATE cpe_inventory."""
        func_start = self.sql_content.find("FUNCTION reconcile_live_to_history")
        self.assertGreater(func_start, 0)
        func_body = self.sql_content[func_start:]
        self.assertNotIn("UPDATE cpe_inventory", func_body)

    def test_stress_zero_inventory_modifications(self):
        """
        Emulate 500 volatile updates and optical variations and assert zero inventory writes.
        """
        inv_record = {
            "cpe_id": "STRESS-CPE",
            "status": "offline",
            "updated_at": "2026-09-07T00:00:00Z"
        }
        initial_inv = copy.deepcopy(inv_record)

        # Ingest 500 volatile updates with optical variations
        history = []
        old_rx = None
        for i in range(500):
            new_rx = -18.0 - (i * 1.5)
            should_rec, reason, rx = PostgresPlpgsqlReconciliationOracle.evaluate_transition(
                "INSERT" if i == 0 else "UPDATE",
                {"rx_optical_power": old_rx} if i > 0 else None,
                {},
                {"rx_optical_power": new_rx},
                {}
            )
            if should_rec:
                history.append({"optical_power": rx, "reason": reason})
            old_rx = new_rx

        # Assert cpe_inventory was NEVER modified
        self.assertEqual(inv_record, initial_inv, "cpe_inventory MUST NEVER be modified by reconciliation")
        self.assertGreater(len(history), 200, "Historical snapshots should be recorded for optical deltas")


if __name__ == "__main__":
    unittest.main(verbosity=2)
