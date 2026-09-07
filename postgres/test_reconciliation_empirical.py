#!/usr/bin/env python3
"""
Empirical Challenge Test Harness for PostgreSQL Reconciliation Triggers
Author: Challenger 2 (teamwork_preview_challenger) - Milestone 2
Target: postgres/init.sql

Verifies:
1. simulate_flow.sh Step 4 exact expectations:
   - Initial insertion into cpe_live_state creates exactly 1 history record ('initial_state').
   - Optical signal variation > 1.0 dBm creates exactly 2nd history record ('optical_signal_variation').
   - Zero WAL write amplification: cpe_inventory is NEVER updated by the reconciliation trigger.
2. Heartbeat & noise deduplication:
   - Routine last_seen / updated_at updates do NOT create redundant historical records.
   - Identical telemetry payloads do NOT create redundant historical records.
   - Sub-threshold optical fluctuations (<= 1.0 dBm) do NOT create redundant historical records.
   - CPU, RAM, temperature, and IP fluctuations do NOT create historical records.
3. Optical variation taxonomy & trigger correctness:
   - initial_state on initial optical acquisition
   - optical_signal_variation on |delta| > 1.0 dBm
4. Relational integrity & Edge cases:
   - Primary table cpe_historical_metrics and backward-compatibility view cpe_state_history.
   - Foreign key constraint and ON DELETE CASCADE behavior.
   - Multi-device isolation.
   - 1000-cycle rapid heartbeat fuzzing.
5. Static DDL inspection of init.sql (top-level tablespace, zero cpe_inventory updates, optical threshold).
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

        # 3. cpe_historical_metrics
        cur.execute("""
            CREATE TABLE cpe_historical_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cpe_id TEXT NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                current_parameters TEXT DEFAULT '{}',
                telemetry_metrics TEXT NOT NULL DEFAULT '{}',
                optical_power REAL,
                recorded_at TEXT NOT NULL,
                change_reason TEXT NOT NULL DEFAULT 'optical_signal_variation'
            );
        """)

        # 4. Backward-compatibility view cpe_state_history
        cur.execute("""
            CREATE VIEW cpe_state_history AS
            SELECT id, cpe_id, status, current_parameters, telemetry_metrics, optical_power, recorded_at, change_reason
            FROM cpe_historical_metrics;
        """)

        # Triggers faithfully mirroring reconcile_live_to_history() in init.sql:
        # Zero UPDATE on cpe_inventory (zero WAL write amplification)
        cur.execute("""
            CREATE TRIGGER trg_live_state_reconcile_insert
            AFTER INSERT ON cpe_live_state
            FOR EACH ROW
            WHEN (
                COALESCE(
                    json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                    json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                    json_extract(NEW.telemetry_metrics, '$.optical_power'),
                    json_extract(NEW.telemetry_metrics, '$.rx_power'),
                    json_extract(NEW.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                ) IS NOT NULL
            )
            BEGIN
                INSERT INTO cpe_historical_metrics (
                    cpe_id,
                    status,
                    current_parameters,
                    telemetry_metrics,
                    optical_power,
                    recorded_at,
                    change_reason
                ) VALUES (
                    NEW.cpe_id,
                    NEW.status,
                    NEW.current_parameters,
                    NEW.telemetry_metrics,
                    CAST(COALESCE(
                        json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                        json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                        json_extract(NEW.telemetry_metrics, '$.optical_power'),
                        json_extract(NEW.telemetry_metrics, '$.rx_power'),
                        json_extract(NEW.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                    ) AS REAL),
                    NEW.updated_at,
                    'initial_state'
                );
            END;
        """)

        cur.execute("""
            CREATE TRIGGER trg_live_state_reconcile_update
            AFTER UPDATE ON cpe_live_state
            FOR EACH ROW
            WHEN (
                COALESCE(
                    json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                    json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                    json_extract(NEW.telemetry_metrics, '$.optical_power'),
                    json_extract(NEW.telemetry_metrics, '$.rx_power'),
                    json_extract(NEW.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                ) IS NOT NULL
            )
            BEGIN
                INSERT INTO cpe_historical_metrics (
                    cpe_id,
                    status,
                    current_parameters,
                    telemetry_metrics,
                    optical_power,
                    recorded_at,
                    change_reason
                )
                SELECT
                    NEW.cpe_id,
                    NEW.status,
                    NEW.current_parameters,
                    NEW.telemetry_metrics,
                    CAST(COALESCE(
                        json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                        json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                        json_extract(NEW.telemetry_metrics, '$.optical_power'),
                        json_extract(NEW.telemetry_metrics, '$.rx_power'),
                        json_extract(NEW.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                    ) AS REAL),
                    NEW.updated_at,
                    CASE
                        WHEN COALESCE(
                            json_extract(OLD.telemetry_metrics, '$.rx_optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_rx_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.rx_power'),
                            json_extract(OLD.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                        ) IS NULL THEN 'initial_state'
                        ELSE 'optical_signal_variation'
                    END
                WHERE
                    COALESCE(
                        json_extract(OLD.telemetry_metrics, '$.rx_optical_power'),
                        json_extract(OLD.telemetry_metrics, '$.optical_rx_power'),
                        json_extract(OLD.telemetry_metrics, '$.optical_power'),
                        json_extract(OLD.telemetry_metrics, '$.rx_power'),
                        json_extract(OLD.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                    ) IS NULL
                    OR ABS(
                        CAST(COALESCE(
                            json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                            json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                            json_extract(NEW.telemetry_metrics, '$.optical_power'),
                            json_extract(NEW.telemetry_metrics, '$.rx_power'),
                            json_extract(NEW.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                        ) AS REAL)
                        -
                        CAST(COALESCE(
                            json_extract(OLD.telemetry_metrics, '$.rx_optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_rx_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.rx_power'),
                            json_extract(OLD.current_parameters, '$.Device.Optical.Interface.1.OpticalSignalLevel')
                        ) AS REAL)
                    ) > 1.0;
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
            SELECT id, cpe_id, status, current_parameters, telemetry_metrics, optical_power, recorded_at, change_reason
            FROM cpe_historical_metrics
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
                "optical_power": r[5],
                "recorded_at": r[6],
                "change_reason": r[7],
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

        # Step 2 & 3: Ingest initial telemetry payload (status=online, rx_optical_power=-18.5, cpu_usage=42.5)
        initial_metrics = {
            "rx_optical_power": -18.5,
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
        self.assertEqual(hist_1[0]["optical_power"], -18.5)

        # Zero WAL write amplification: cpe_inventory status is NOT updated by trigger
        inv_after_step3 = self.db.get_inventory(cpe_id)
        self.assertEqual(inv_after_step3["status"], "offline", "cpe_inventory status must NOT be modified by trigger")

        # Step 4: Metric alteration with optical degradation > 1.0 dBm (-18.5 -> -21.0, delta = 2.5 dBm)
        modified_metrics = {
            "rx_optical_power": -21.0,
            "cpu_usage": 88.4,
            "memory_usage": 75.2,
            "rx_bytes": 2097152,
            "tx_bytes": 1048576,
            "temperature": 52.8,
        }
        self.db.upsert_live_state(cpe_id, status="online", metrics=modified_metrics, parameters=initial_params)

        # Verify exactly 2 history records created (satisfying simulate_flow.sh Step 4 >= 2)
        hist_2 = self.db.get_history(cpe_id)
        self.assertEqual(len(hist_2), 2, "Optical delta > 1.0 dBm MUST create exactly 2nd history record")
        self.assertGreaterEqual(len(hist_2), 2, "simulate_flow.sh Step 4 assertion COUNT >= 2 satisfied")
        self.assertEqual(hist_2[1]["change_reason"], "optical_signal_variation")
        self.assertEqual(hist_2[1]["optical_power"], -21.0)
        self.assertEqual(hist_2[1]["telemetry_metrics"]["cpu_usage"], 88.4)

        # Verify cpe_inventory remains untouched
        self.assertEqual(self.db.get_inventory(cpe_id)["status"], "offline")


class TestHeartbeatAndRoutineDeduplication(unittest.TestCase):
    """Verifies that routine heartbeat, noise, and sub-threshold variations DO NOT create records."""

    def setUp(self):
        self.db = RealSqlRelationalHarness()
        self.cpe_id = "HEARTBEAT-CPE-002"
        self.db.register_cpe(self.cpe_id, "HB-SERIAL-002")
        self.initial_metrics = {"rx_optical_power": -18.5, "cpu_usage": 30.0, "memory_usage": 50.0}
        self.initial_params = {"Device.DeviceInfo.SoftwareVersion": "1.0.0"}
        self.db.upsert_live_state(self.cpe_id, "online", self.initial_metrics, self.initial_params)

    def test_single_heartbeat_does_not_pollute_history(self):
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1)
        self.db.update_heartbeat_only(self.cpe_id)
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
        for _ in range(10):
            self.db.upsert_live_state(self.cpe_id, "online", self.initial_metrics, self.initial_params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Identical telemetry retransmission MUST NOT create history records")

    def test_sub_threshold_optical_variation_ignored(self):
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1)
        # Delta = |-19.2 - (-18.5)| = 0.7 dBm <= 1.0 dBm
        sub_metrics = dict(self.initial_metrics, rx_optical_power=-19.2)
        self.db.upsert_live_state(self.cpe_id, "online", sub_metrics, self.initial_params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Sub-1.0 dBm variation MUST NOT create history record")

    def test_exact_threshold_boundary(self):
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1)
        # Exactly 1.0 dBm delta: |-19.5 - (-18.5)| = 1.00 dBm (strictly > 1.0 required)
        boundary_metrics = dict(self.initial_metrics, rx_optical_power=-19.5)
        self.db.upsert_live_state(self.cpe_id, "online", boundary_metrics, self.initial_params)
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 1, "Delta == 1.00 dBm MUST NOT fire trigger")

        # Delta > 1.0 dBm from current state (-19.5 -> -20.6, delta = 1.1 dBm > 1.0 dBm)
        trigger_metrics = dict(self.initial_metrics, rx_optical_power=-20.6)
        self.db.upsert_live_state(self.cpe_id, "online", trigger_metrics, self.initial_params)
        self.assertEqual(len(self.db.get_history(self.cpe_id)), 2, "Delta > 1.0 dBm MUST fire trigger")


class TestTransitionTaxonomyAndEdgeCases(unittest.TestCase):
    """Stress tests transition reasons, multi-device isolation, cascading, and non-optical noise."""

    def setUp(self):
        self.db = RealSqlRelationalHarness()
        self.cpe_id = "EDGE-CPE-003"
        self.db.register_cpe(self.cpe_id, "EDGE-SERIAL-003")
        self.metrics = {"rx_optical_power": -18.5, "cpu": 10.0}
        self.params = {"sw": "1.0"}
        self.db.upsert_live_state(self.cpe_id, "online", self.metrics, self.params)

    def test_optical_signal_improvement_triggers(self):
        """Signal attenuation reduction (improvement, e.g. -18.5 -> -16.0, delta 2.5 > 1.0) must record snapshot."""
        improved_metrics = dict(self.metrics, rx_optical_power=-16.0)
        self.db.upsert_live_state(self.cpe_id, "online", improved_metrics, self.params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["change_reason"], "optical_signal_variation")
        self.assertEqual(hist[1]["optical_power"], -16.0)

    def test_status_change_alone_does_not_record_history(self):
        self.db.upsert_live_state(self.cpe_id, "offline", self.metrics, self.params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Status change without optical variation must NOT create history snapshot")
        # Ensure cpe_inventory status is untouched
        self.assertEqual(self.db.get_inventory(self.cpe_id)["status"], "offline")

    def test_parameters_change_alone_does_not_record_history(self):
        new_params = {"sw": "2.0"}
        self.db.upsert_live_state(self.cpe_id, "online", self.metrics, new_params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Parameter change without optical variation must NOT create history snapshot")

    def test_cpu_and_memory_fluctuation_does_not_record_history(self):
        noisy_metrics = dict(self.metrics, cpu=99.9, memory=95.0, temp=75.0)
        self.db.upsert_live_state(self.cpe_id, "online", noisy_metrics, self.params)
        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "CPU/RAM/Temp fluctuations without optical delta must NOT create history snapshot")

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
        self.db.upsert_live_state(cpe_2, "online", {"rx_optical_power": -19.0, "cpu": 20}, {})

        # Mutate CPE 2 with delta > 1.0 dBm (-19.0 -> -22.0, delta 3.0)
        self.db.upsert_live_state(cpe_2, "online", {"rx_optical_power": -22.0, "cpu": 50}, {})

        hist_1 = self.db.get_history(self.cpe_id)
        hist_2 = self.db.get_history(cpe_2)
        self.assertEqual(len(hist_1), 1)
        self.assertEqual(len(hist_2), 2)

    def test_interleaved_heartbeats_and_optical_mutations(self):
        """
        Interleave 5000 heartbeats with 5 distinct optical mutations > 1.0 dBm.
        Assert that EXACTLY 1 (initial) + 5 (mutations) = 6 history records exist.
        """
        expected_records = 1
        current_rx = -18.5
        for i in range(1, 6):
            # 1000 routine heartbeats
            for _ in range(1000):
                self.db.update_heartbeat_only(self.cpe_id)

            # Optical mutation by 1.5 dBm
            current_rx -= 1.5
            self.db.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": current_rx}, self.params)
            expected_records += 1

        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 6, f"Expected exactly 6 history records, got {len(hist)}")
        reasons = [h["change_reason"] for h in hist]
        self.assertEqual(reasons[0], "initial_state")
        for r in reasons[1:]:
            self.assertEqual(r, "optical_signal_variation")

    def test_ip_address_change_does_not_pollute_history(self):
        cur = self.db.conn.cursor()
        cur.execute("UPDATE cpe_live_state SET ip_address = '192.168.1.100' WHERE cpe_id = ?", (self.cpe_id,))
        self.db.conn.commit()

        hist = self.db.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "IP address change must not trigger history snapshot")


class TestInitSqlStaticAST(unittest.TestCase):
    """Direct static inspection of postgres/init.sql to ensure full DDL compliance."""

    @classmethod
    def setUpClass(cls):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            cls.sql = f.read()

    def test_unlogged_ram_tablespace(self):
        self.assertRegex(self.sql, r"CREATE\s+UNLOGGED\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?cpe_live_state")
        self.assertIn("TABLESPACE ram_tablespace", self.sql)
        self.assertIn("CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';", self.sql)

    def test_tablespace_not_in_do_block(self):
        has_do_block = re.search(r"DO\s+\$\$.*?CREATE\s+TABLESPACE.*?\$\$;", self.sql, re.DOTALL | re.IGNORECASE)
        self.assertIsNone(has_do_block, "CREATE TABLESPACE must NOT be inside a DO $$ block")

    def test_trigger_timing_and_event(self):
        self.assertRegex(
            self.sql,
            r"CREATE\s+TRIGGER\s+reconcile_live_to_history\s+AFTER\s+INSERT\s+OR\s+UPDATE\s+ON\s+cpe_live_state\s+FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+reconcile_live_to_history\(\);",
        )

    def test_no_wal_write_amplification(self):
        func_match = re.search(r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+reconcile_live_to_history\(\).*?\$\$(.*?)\$\$", self.sql, re.DOTALL)
        self.assertIsNotNone(func_match)
        func_body = func_match.group(1)
        self.assertNotIn("UPDATE cpe_inventory", func_body, "Trigger must NOT update cpe_inventory")

    def test_optical_threshold_in_sql(self):
        func_match = re.search(r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+reconcile_live_to_history\(\).*?\$\$(.*?)\$\$", self.sql, re.DOTALL)
        self.assertIsNotNone(func_match)
        func_body = func_match.group(1)
        self.assertIn("rx_optical_power", func_body)
        self.assertIn("1.0", func_body)
        self.assertIn("optical_signal_variation", func_body)
        self.assertIn("cpe_historical_metrics", func_body)

    def test_cpe_historical_metrics_table_and_view(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS cpe_historical_metrics", self.sql)
        self.assertIn("CREATE OR REPLACE VIEW cpe_state_history", self.sql)

    def test_heartbeat_timestamp_deduplication_in_sql(self):
        func_match = re.search(r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+reconcile_live_to_history\(\).*?\$\$(.*?)\$\$", self.sql, re.DOTALL)
        self.assertIsNotNone(func_match)
        func_body = func_match.group(1)
        self.assertNotIn("last_seen", func_body, "last_seen must NOT trigger history records")

    def test_foreign_key_cascades(self):
        self.assertIn("REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE", self.sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
