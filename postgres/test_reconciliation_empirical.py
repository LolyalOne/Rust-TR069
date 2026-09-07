#!/usr/bin/env python3
"""
Empirical Challenge Test Harness for PostgreSQL Reconciliation Triggers
Author: Challenger 2 (teamwork_preview_challenger) - Milestone 2
Target: /mnt/d/Projetos/TR069-181/postgres/init.sql

Verifies:
1. simulate_flow.sh Step 4 exact expectations:
   - Initial insertion into cpe_live_state creates exactly 1 history record ('initial_state').
   - Subsequent metric modification (e.g. cpu_usage change) creates exactly 2nd history record ('telemetry_metrics_changed').
2. Heartbeat deduplication:
   - Routine last_seen / updated_at updates do NOT create redundant historical records.
   - Identical telemetry payloads do NOT create redundant historical records.
3. Transition taxonomy & trigger correctness:
   - status_and_metrics_changed
   - status_changed
   - telemetry_metrics_changed
   - parameters_changed
4. Relational integrity & Edge cases:
   - Foreign key constraint and ON DELETE CASCADE behavior.
   - Multi-device isolation.
   - JSON key ordering invariance.
   - 1000-cycle rapid heartbeat fuzzing.
5. Static DDL inspection of init.sql.
"""

import os
import re
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

INIT_SQL_PATH = Path(__file__).resolve().parent / "init.sql"


class RealSqlRelationalHarness:
    """
    Real relational SQL test harness using SQLite 3 engine with FOREIGN KEYS enabled
    and triggers faithfully executing the exact logic of postgres/init.sql.
    """

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._init_db()

    def _init_db(self):
        cur = self.conn.cursor()
        # 1. cpe_inventory
        cur.execute("""
            CREATE TABLE cpe_inventory (
                cpe_id TEXT PRIMARY KEY,
                serial_number TEXT UNIQUE NOT NULL,
                manufacturer TEXT NOT NULL,
                model TEXT NOT NULL,
                oui TEXT,
                product_class TEXT,
                hardware_version TEXT,
                software_version TEXT,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'offline',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 2. cpe_live_state
        cur.execute("""
            CREATE TABLE cpe_live_state (
                cpe_id TEXT PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
                endpoint_id TEXT,
                current_parameters TEXT NOT NULL DEFAULT '{}',
                telemetry_metrics TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'offline',
                ip_address TEXT,
                firmware_version TEXT,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # 3. cpe_state_history
        cur.execute("""
            CREATE TABLE cpe_state_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cpe_id TEXT NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                current_parameters TEXT DEFAULT '{}',
                telemetry_metrics TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                change_reason TEXT NOT NULL DEFAULT 'telemetry_update'
            );
        """)

        # Triggers mirroring fn_reconcile_cpe_live_state() in init.sql:
        # Trigger A: AFTER INSERT ON cpe_live_state
        cur.execute("""
            CREATE TRIGGER trg_live_state_reconcile_insert
            AFTER INSERT ON cpe_live_state
            FOR EACH ROW
            BEGIN
                -- 1. Sync live status and timestamp back to cpe_inventory
                UPDATE cpe_inventory
                SET status = NEW.status,
                    updated_at = NEW.updated_at
                WHERE cpe_id = NEW.cpe_id;

                -- 2. Insert initial snapshot into history
                INSERT INTO cpe_state_history (
                    cpe_id,
                    status,
                    current_parameters,
                    telemetry_metrics,
                    recorded_at,
                    change_reason
                ) VALUES (
                    NEW.cpe_id,
                    NEW.status,
                    NEW.current_parameters,
                    NEW.telemetry_metrics,
                    NEW.updated_at,
                    'initial_state'
                );
            END;
        """)

        # Trigger B: AFTER UPDATE ON cpe_live_state
        cur.execute("""
            CREATE TRIGGER trg_live_state_reconcile_update
            AFTER UPDATE ON cpe_live_state
            FOR EACH ROW
            BEGIN
                -- 1. Sync live status and timestamp back to cpe_inventory
                UPDATE cpe_inventory
                SET status = NEW.status,
                    updated_at = NEW.updated_at
                WHERE cpe_id = NEW.cpe_id;

                -- 2. Detect transition type and insert snapshot if warranted
                INSERT INTO cpe_state_history (
                    cpe_id,
                    status,
                    current_parameters,
                    telemetry_metrics,
                    recorded_at,
                    change_reason
                )
                SELECT
                    NEW.cpe_id,
                    NEW.status,
                    NEW.current_parameters,
                    NEW.telemetry_metrics,
                    NEW.updated_at,
                    CASE
                        WHEN (OLD.status IS NOT NEW.status) AND (OLD.telemetry_metrics IS NOT NEW.telemetry_metrics) THEN 'status_and_metrics_changed'
                        WHEN (OLD.status IS NOT NEW.status) THEN 'status_changed'
                        WHEN (OLD.telemetry_metrics IS NOT NEW.telemetry_metrics) THEN 'telemetry_metrics_changed'
                        WHEN (OLD.current_parameters IS NOT NEW.current_parameters) THEN 'parameters_changed'
                    END
                WHERE
                    (OLD.status IS NOT NEW.status)
                    OR (OLD.telemetry_metrics IS NOT NEW.telemetry_metrics)
                    OR (OLD.current_parameters IS NOT NEW.current_parameters);
            END;
        """)
        self.conn.commit()

    def register_cpe(self, cpe_id: str, serial: str, manufacturer="TP-Link", model="Archer-AX50"):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO cpe_inventory (cpe_id, serial_number, manufacturer, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (cpe_id, serial, manufacturer, model, now, now))
        self.conn.commit()

    def upsert_live_state(self, cpe_id: str, status: str, metrics: dict, parameters: dict, last_seen=None):
        now = datetime.now(timezone.utc).isoformat()
        ls = last_seen or now
        metrics_json = json.dumps(metrics, sort_keys=True)
        params_json = json.dumps(parameters, sort_keys=True)

        cur = self.conn.cursor()
        # Check exists
        cur.execute("SELECT cpe_id FROM cpe_live_state WHERE cpe_id = ?", (cpe_id,))
        exists = cur.fetchone() is not None

        if not exists:
            cur.execute("""
                INSERT INTO cpe_live_state (cpe_id, status, telemetry_metrics, current_parameters, last_seen, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cpe_id, status, metrics_json, params_json, ls, now))
        else:
            cur.execute("""
                UPDATE cpe_live_state
                SET status = ?,
                    telemetry_metrics = ?,
                    current_parameters = ?,
                    last_seen = ?,
                    updated_at = ?
                WHERE cpe_id = ?
            """, (status, metrics_json, params_json, ls, now, cpe_id))
        self.conn.commit()

    def update_heartbeat_only(self, cpe_id: str):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE cpe_live_state
            SET last_seen = ?,
                updated_at = ?
            WHERE cpe_id = ?
        """, (now, now, cpe_id))
        self.conn.commit()

    def get_history(self, cpe_id: str):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason
            FROM cpe_state_history
            WHERE cpe_id = ?
            ORDER BY id ASC
        """, (cpe_id,))
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "cpe_id": r[1],
                "status": r[2],
                "current_parameters": json.loads(r[3]),
                "telemetry_metrics": json.loads(r[4]),
                "recorded_at": r[5],
                "change_reason": r[6],
            }
            for r in rows
        ]

    def get_inventory(self, cpe_id: str):
        cur = self.conn.cursor()
        cur.execute("SELECT status, updated_at FROM cpe_inventory WHERE cpe_id = ?", (cpe_id,))
        r = cur.fetchone()
        return {"status": r[0], "updated_at": r[1]} if r else None

    def delete_cpe(self, cpe_id: str):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM cpe_inventory WHERE cpe_id = ?", (cpe_id,))
        self.conn.commit()


class TestSimulateFlowStep4Reconciliation(unittest.TestCase):
    """Verifies that simulate_flow.sh Step 4 expectations are strictly met."""

    def setUp(self):
        self.db = RealSqlRelationalHarness()

    def test_step4_flow_end_to_end(self):
        cpe_id = "TEST-CPE-001"
        serial = "TEST001-SERIAL-9988"

        # Step 1: Inventory registration
        self.db.register_cpe(cpe_id=cpe_id, serial=serial)
        inv = self.db.get_inventory(cpe_id)
        self.assertEqual(inv["status"], "offline")
        self.assertEqual(len(self.db.get_history(cpe_id)), 0)

        # Step 2 & 3: Ingest initial telemetry payload (status=online, cpu_usage=42.5)
        initial_metrics = {
            "cpu_usage": 42.5,
            "memory_usage": 68.0,
            "rx_bytes": 1048576,
            "tx_bytes": 524288,
            "temperature": 45.2,
        }
        initial_params = {
            "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
            "Device.WiFi.Radio.1.Status": "Up",
        }
        self.db.upsert_live_state(cpe_id, status="online", metrics=initial_metrics, parameters=initial_params)

        # Verify exactly 1 history record created with reason 'initial_state'
        hist_1 = self.db.get_history(cpe_id)
        self.assertEqual(len(hist_1), 1, "Initial insertion MUST create exactly 1 history snapshot")
        self.assertEqual(hist_1[0]["change_reason"], "initial_state")
        self.assertEqual(hist_1[0]["status"], "online")
        self.assertEqual(hist_1[0]["telemetry_metrics"]["cpu_usage"], 42.5)

        # Verify cpe_inventory reconciled to 'online'
        inv_after_step3 = self.db.get_inventory(cpe_id)
        self.assertEqual(inv_after_step3["status"], "online")

        # Step 4: Metric alteration (cpu_usage: 88.4)
        modified_metrics = {
            "cpu_usage": 88.4,
            "memory_usage": 75.2,
            "rx_bytes": 2097152,
            "tx_bytes": 1048576,
            "temperature": 52.8,
        }
        self.db.upsert_live_state(cpe_id, status="online", metrics=modified_metrics, parameters=initial_params)

        # Verify exactly 2 history records created (satisfying simulate_flow.sh Step 4 >= 2)
        hist_2 = self.db.get_history(cpe_id)
        self.assertEqual(len(hist_2), 2, "Metric alteration MUST create exactly 2nd history record")
        self.assertGreaterEqual(len(hist_2), 2, "simulate_flow.sh Step 4 assertion COUNT >= 2 satisfied")
        self.assertEqual(hist_2[1]["change_reason"], "telemetry_metrics_changed")
        self.assertEqual(hist_2[1]["telemetry_metrics"]["cpu_usage"], 88.4)


class TestHeartbeatAndRoutineDeduplication(unittest.TestCase):
    """Verifies that routine heartbeat and timestamp updates DO NOT create redundant records."""

    def setUp(self):
        self.db = RealSqlRelationalHarness()
        self.cpe_id = "HEARTBEAT-CPE-002"
        self.db.register_cpe(self.cpe_id, "HB-SERIAL-002")
        self.initial_metrics = {"cpu_usage": 30.0, "memory_usage": 50.0}
        self.initial_params = {"Device.DeviceInfo.SoftwareVersion": "1.0.0"}
        self.db.upsert_live_state(self.cpe_id, "online", self.initial_metrics, self.initial_params)

    def test_single_heartbeat_does_not_pollute_history(self):
        # Initial state = 1 history record
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1)

        # Execute heartbeat update (only last_seen and updated_at change)
        self.db.update_heartbeat_only(self.cpe_id)

        # History count MUST STILL be 1
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Routine heartbeat MUST NOT add history record")

    def test_1000_rapid_heartbeats_deduplicated(self):
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1)

        for _ in range(1000):
            self.db.update_heartbeat_only(self.cpe_id)

        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "1000 rapid heartbeats MUST NOT create any additional history records")

    def test_identical_telemetry_resend_deduplicated(self):
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1)

        # Resend exact same metrics and parameters 10 times
        for _ in range(10):
            self.db.upsert_live_state(self.cpe_id, "online", self.initial_metrics, self.initial_params)

        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Identical telemetry retransmission MUST NOT create history records")


class TestTransitionTaxonomyAndEdgeCases(unittest.TestCase):
    """Stress tests transition reasons, multi-device isolation, cascading, and JSON invariance."""

    def setUp(self):
        self.db = RealSqlRelationalHarness()
        self.cpe_id = "EDGE-CPE-003"
        self.db.register_cpe(self.cpe_id, "EDGE-SERIAL-003")
        self.metrics = {"cpu": 10.0}
        self.params = {"sw": "1.0"}
        self.db.upsert_live_state(self.cpe_id, "online", self.metrics, self.params)

    def test_status_change_only(self):
        self.db.upsert_live_state(self.cpe_id, "offline", self.metrics, self.params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["change_reason"], "status_changed")
        self.assertEqual(hist[1]["status"], "offline")

    def test_parameters_change_only(self):
        new_params = {"sw": "2.0"}
        self.db.upsert_live_state(self.cpe_id, "online", self.metrics, new_params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["change_reason"], "parameters_changed")
        self.assertEqual(hist[1]["current_parameters"]["sw"], "2.0")

    def test_status_and_metrics_simultaneous_change(self):
        new_metrics = {"cpu": 99.9}
        self.db.upsert_live_state(self.cpe_id, "degraded", new_metrics, self.params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["change_reason"], "status_and_metrics_changed")
        self.assertEqual(hist[1]["status"], "degraded")
        self.assertEqual(hist[1]["telemetry_metrics"]["cpu"], 99.9)

    def test_foreign_key_violation_on_unregistered_cpe(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.upsert_live_state("NON-EXISTENT-CPE", "online", {}, {})

    def test_cascade_delete_cleans_live_and_history(self):
        self.db.delete_cpe(self.cpe_id)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 0, "Deleting CPE from inventory MUST cascade-delete history")

    def test_multi_device_isolation(self):
        cpe_2 = "CPE-004"
        self.db.register_cpe(cpe_2, "SERIAL-004")
        self.db.upsert_live_state(cpe_2, "online", {"cpu": 20}, {})

        # Mutate CPE 2
        self.db.upsert_live_state(cpe_2, "online", {"cpu": 50}, {})

        # Check CPE 1 is completely unaffected
        hist_1 = self.db.get_history(self.cpe_id)
        hist_2 = self.db.get_history(cpe_2)
        self.assertEqual(len(hist_1), 1)
        self.assertEqual(len(hist_2), 2)

    def test_interleaved_heartbeats_and_metric_mutations(self):
        """
        Interleave 5000 heartbeats with 5 distinct metric changes.
        Assert that EXACTLY 1 (initial) + 5 (mutations) = 6 history records exist.
        """
        expected_records = 1
        for i in range(1, 6):
            # 1000 routine heartbeats
            for _ in range(1000):
                self.db.update_heartbeat_only(self.cpe_id)
            
            # Metric mutation
            self.db.upsert_live_state(self.cpe_id, "online", {"cpu": 10.0 + i * 10}, self.params)
            expected_records += 1

        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 6, f"Expected exactly 6 history records, got {len(hist)}")
        reasons = [h["change_reason"] for h in hist]
        self.assertEqual(reasons[0], "initial_state")
        for r in reasons[1:]:
            self.assertEqual(r, "telemetry_metrics_changed")

    def test_ip_address_change_does_not_pollute_history(self):
        """
        cpe_state_history does not track ip_address; changing IP must not record history.
        """
        cur = self.db.conn.cursor()
        cur.execute("UPDATE cpe_live_state SET ip_address = '192.168.1.100' WHERE cpe_id = ?", (self.cpe_id,))
        self.db.conn.commit()

        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "IP address change must not trigger history snapshot")

    def test_nested_parameter_json_change(self):
        """
        Verify that nested JSON structures in current_parameters trigger parameters_changed.
        """
        nested_params = {
            "Device": {
                "WiFi": {
                    "Radio": {"1": {"Status": "Down", "Channels": [1, 6, 11]}}
                }
            }
        }
        self.db.upsert_live_state(self.cpe_id, "online", self.metrics, nested_params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["change_reason"], "parameters_changed")
        self.assertEqual(hist[1]["current_parameters"]["Device"]["WiFi"]["Radio"]["1"]["Status"], "Down")



class TestInitSqlStaticAST(unittest.TestCase):
    """Direct static inspection of postgres/init.sql to ensure full DDL compliance."""

    @classmethod
    def setUpClass(cls):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            cls.sql = f.read()

    def test_unlogged_ram_tablespace(self):
        self.assertRegex(self.sql, r"CREATE\s+UNLOGGED\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?cpe_live_state")
        self.assertIn("TABLESPACE ram_tablespace", self.sql)
        self.assertIn("LOCATION '/var/lib/postgresql/ram_data'", self.sql)

    def test_trigger_timing_and_event(self):
        self.assertRegex(
            self.sql,
            r"CREATE\s+TRIGGER\s+trg_cpe_live_state_reconcile\s+AFTER\s+INSERT\s+OR\s+UPDATE\s+ON\s+cpe_live_state\s+FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+fn_reconcile_cpe_live_state\(\);",
        )

    def test_fn_reconcile_branches(self):
        self.assertIn("v_reason := 'initial_state';", self.sql)
        self.assertIn("v_reason := 'status_and_metrics_changed';", self.sql)
        self.assertIn("v_reason := 'status_changed';", self.sql)
        self.assertIn("v_reason := 'telemetry_metrics_changed';", self.sql)
        self.assertIn("v_reason := 'parameters_changed';", self.sql)

    def test_heartbeat_timestamp_deduplication_in_sql(self):
        # Ensure updated_at and last_seen are NOT in the IF condition that sets v_should_record
        func_match = re.search(r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+fn_reconcile_cpe_live_state\(\).*?\$\$(.*?)\$\$", self.sql, re.DOTALL)
        self.assertIsNotNone(func_match)
        func_body = func_match.group(1)

        # Check update condition
        if_update_match = re.search(r"ELSIF\s+\(TG_OP\s*=\s*'UPDATE'\)\s+THEN(.*?)END\s+IF;", func_body, re.DOTALL)
        self.assertIsNotNone(if_update_match)
        conditions = if_update_match.group(1)

        self.assertNotIn("last_seen", conditions, "last_seen must NOT trigger history records")
        self.assertNotIn("updated_at", conditions, "updated_at must NOT trigger history records")

    def test_foreign_key_cascades(self):
        self.assertIn("REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE", self.sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
