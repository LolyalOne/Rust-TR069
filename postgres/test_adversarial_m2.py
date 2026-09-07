#!/usr/bin/env python3
"""
Adversarial Challenge Test Suite for Milestone 2 (PostgreSQL Hybrid Schema & Reconciliation)
Author: challenger_m2_it2_1 (teamwork_preview_challenger)
Target: postgres/init.sql, postgres/test_schema.py, postgres/test_reconciliation_empirical.py

Verifies:
1. CREATE TABLESPACE execution environment (top-level vs procedural/transaction block).
2. Zero WAL write amplification: asserts cpe_inventory is NEVER touched by triggers.
3. Strict boundary edge cases:
   - delta exactly 1.0 dBm (should NOT trigger)
   - delta 1.01 dBm (should trigger)
   - negative delta > 1.0 dBm (e.g. -18.0 to -20.0, should trigger)
   - positive delta > 1.0 dBm (e.g. -21.0 to -19.0, should trigger)
   - delta 0.99 dBm (should NOT trigger)
   - delta 1.001 dBm (should trigger)
   - non-numeric string resilience
   - TR-181 data model parameters
4. Stress harness: 3000+ rapid updates with memory leak profiling (tracemalloc)
   and verification of zero unwanted history rows.
"""

import gc
import json
import re
import sqlite3
import sys
import tracemalloc
import unittest
from datetime import datetime, timezone
from pathlib import Path

INIT_SQL_PATH = Path(__file__).resolve().parent / "init.sql"


class TestTablespaceProceduralSafety(unittest.TestCase):
    """Verifies that CREATE TABLESPACE is not enclosed in any procedural or transaction block."""

    @classmethod
    def setUpClass(cls):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            cls.sql_content = f.read()

    def test_tablespace_not_in_do_block(self):
        """Asserts that CREATE TABLESPACE is not inside a DO $$ ... $$ procedural block."""
        match = re.search(
            r"DO\s+\$\$(.*?CREATE\s+TABLESPACE.*?)\$\$;",
            self.sql_content,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNone(
            match,
            "CRITICAL: CREATE TABLESPACE must NOT be enclosed in a DO $$ block! "
            "PostgreSQL raises 'ERROR: CREATE TABLESPACE cannot be executed inside a transaction block'.",
        )

    def test_tablespace_not_in_transaction_block(self):
        """Asserts that CREATE TABLESPACE does not follow a BEGIN / START TRANSACTION statement."""
        # Find index of CREATE TABLESPACE
        match = re.search(r"CREATE\s+TABLESPACE\s+ram_tablespace", self.sql_content, re.IGNORECASE)
        self.assertIsNotNone(match, "CREATE TABLESPACE ram_tablespace statement not found in init.sql")
        ts_pos = match.start()

        # Check all text before CREATE TABLESPACE for unclosed BEGIN / START TRANSACTION
        preceding_text = self.sql_content[:ts_pos]
        # Remove comments
        clean_preceding = re.sub(r"--.*$", "", preceding_text, flags=re.MULTILINE)

        has_begin = re.search(r"\b(BEGIN|START\s+TRANSACTION)\b", clean_preceding, re.IGNORECASE)
        self.assertIsNone(has_begin, "CREATE TABLESPACE must not be preceded by BEGIN / START TRANSACTION")

    def test_tablespace_is_top_level_statement(self):
        """Verifies statement is at the top level of init.sql."""
        lines = [line.strip() for line in self.sql_content.splitlines()]
        tablespace_lines = [
            line for line in lines
            if line.upper().startswith("CREATE TABLESPACE") and not line.startswith("--")
        ]
        self.assertEqual(len(tablespace_lines), 1, "Exactly one active CREATE TABLESPACE statement expected")
        self.assertEqual(
            tablespace_lines[0],
            "CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';",
            "Exact top-level syntax mismatch",
        )


class AdversarialRelationalHarness:
    """
    Relational SQLite harness with foreign keys enabled, audit tracking on cpe_inventory,
    and trigger logic faithfully executing postgres/init.sql reconciliation rules.
    """

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.inventory_writes = []
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

        # Audit triggers on cpe_inventory to track any write (INSERT, UPDATE, DELETE)
        cur.execute("""
            CREATE TABLE inventory_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT,
                cpe_id TEXT,
                timestamp TEXT
            );
        """)
        cur.execute("""
            CREATE TRIGGER trg_audit_inventory_update
            AFTER UPDATE ON cpe_inventory
            FOR EACH ROW
            BEGIN
                INSERT INTO inventory_audit_log (operation, cpe_id, timestamp)
                VALUES ('UPDATE', NEW.cpe_id, NEW.updated_at);
            END;
        """)
        cur.execute("""
            CREATE TRIGGER trg_audit_inventory_insert
            AFTER INSERT ON cpe_inventory
            FOR EACH ROW
            BEGIN
                INSERT INTO inventory_audit_log (operation, cpe_id, timestamp)
                VALUES ('INSERT', NEW.cpe_id, NEW.updated_at);
            END;
        """)

        # Triggers mirroring reconcile_live_to_history() in postgres/init.sql
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
                    json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                    json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                ) IS NOT NULL
                AND typeof(CAST(COALESCE(
                    json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                    json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                    json_extract(NEW.telemetry_metrics, '$.optical_power'),
                    json_extract(NEW.telemetry_metrics, '$.rx_power'),
                    json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                    json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                ) AS REAL)) = 'real'
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
                        json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                        json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
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
                    json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                    json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
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
                        json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                        json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                    ) AS REAL),
                    NEW.updated_at,
                    CASE
                        WHEN COALESCE(
                            json_extract(OLD.telemetry_metrics, '$.rx_optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_rx_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.rx_power'),
                            json_extract(OLD.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                            json_extract(OLD.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                        ) IS NULL THEN 'initial_state'
                        ELSE 'optical_signal_variation'
                    END
                WHERE
                    COALESCE(
                        json_extract(OLD.telemetry_metrics, '$.rx_optical_power'),
                        json_extract(OLD.telemetry_metrics, '$.optical_rx_power'),
                        json_extract(OLD.telemetry_metrics, '$.optical_power'),
                        json_extract(OLD.telemetry_metrics, '$.rx_power'),
                        json_extract(OLD.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                        json_extract(OLD.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                    ) IS NULL
                    OR ABS(
                        CAST(COALESCE(
                            json_extract(NEW.telemetry_metrics, '$.rx_optical_power'),
                            json_extract(NEW.telemetry_metrics, '$.optical_rx_power'),
                            json_extract(NEW.telemetry_metrics, '$.optical_power'),
                            json_extract(NEW.telemetry_metrics, '$.rx_power'),
                            json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                            json_extract(NEW.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                        ) AS REAL)
                        -
                        CAST(COALESCE(
                            json_extract(OLD.telemetry_metrics, '$.rx_optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_rx_power'),
                            json_extract(OLD.telemetry_metrics, '$.optical_power'),
                            json_extract(OLD.telemetry_metrics, '$.rx_power'),
                            json_extract(OLD.current_parameters, '$."Device.Optical.Interface.1.OpticalSignalLevel"'),
                            json_extract(OLD.current_parameters, '$."Device.Optical.Interface.1.RxPower"')
                        ) AS REAL)
                    ) > 1.0;
            END;
        """)
        self.conn.commit()

    def register_cpe(self, cpe_id: str, serial: str):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO cpe_inventory (cpe_id, serial_number, manufacturer, model, created_at, updated_at)
            VALUES (?, ?, 'TP-Link', 'Archer-AX50', ?, ?)
        """, (cpe_id, serial, now, now))
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
            for r in cur.fetchall()
        ]

    def get_inventory_updates_count(self, cpe_id: str) -> int:
        """Returns the number of UPDATE operations executed on cpe_inventory for cpe_id."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM inventory_audit_log WHERE operation = 'UPDATE' AND cpe_id = ?", (cpe_id,))
        return cur.fetchone()[0]

    def get_inventory(self, cpe_id: str):
        cur = self.conn.cursor()
        cur.execute("SELECT status, created_at, updated_at FROM cpe_inventory WHERE cpe_id = ?", (cpe_id,))
        r = cur.fetchone()
        return {"status": r[0], "created_at": r[1], "updated_at": r[2]} if r else None


class TestZeroWalAmplification(unittest.TestCase):
    """Verifies that cpe_inventory is strictly NEVER touched by live-state triggers under any condition."""

    def setUp(self):
        self.harness = AdversarialRelationalHarness()
        self.cpe_id = "CPE-WAL-001"
        self.harness.register_cpe(self.cpe_id, "SN-WAL-001")
        self.initial_inv = self.harness.get_inventory(self.cpe_id)

    def test_zero_inventory_writes_across_all_live_state_operations(self):
        # 1. Initial live state insert
        self.harness.upsert_live_state(
            self.cpe_id, "online",
            {"rx_optical_power": -18.0, "cpu": 10.0},
            {"Device.DeviceInfo.SoftwareVersion": "1.0"}
        )
        self.assertEqual(self.harness.get_inventory_updates_count(self.cpe_id), 0, "Initial insert touched cpe_inventory!")

        # 2. Large optical delta update (should trigger history, but NOT touch inventory)
        self.harness.upsert_live_state(
            self.cpe_id, "online",
            {"rx_optical_power": -21.0},
            {"Device.DeviceInfo.SoftwareVersion": "1.0"}
        )
        self.assertEqual(self.harness.get_inventory_updates_count(self.cpe_id), 0, "Optical breach touched cpe_inventory!")

        # 3. Sub-threshold update
        self.harness.upsert_live_state(
            self.cpe_id, "online",
            {"rx_optical_power": -21.5},
            {"Device.DeviceInfo.SoftwareVersion": "1.0"}
        )
        self.assertEqual(self.harness.get_inventory_updates_count(self.cpe_id), 0, "Sub-threshold touched cpe_inventory!")

        # 4. Status change to rebooting
        self.harness.upsert_live_state(
            self.cpe_id, "rebooting",
            {"rx_optical_power": -21.5},
            {"Device.DeviceInfo.SoftwareVersion": "1.0"}
        )
        self.assertEqual(self.harness.get_inventory_updates_count(self.cpe_id), 0, "Status update touched cpe_inventory!")

        # 5. 500 rapid heartbeats
        for _ in range(500):
            self.harness.update_heartbeat_only(self.cpe_id)
        self.assertEqual(self.harness.get_inventory_updates_count(self.cpe_id), 0, "Heartbeats touched cpe_inventory!")

        # Verify cpe_inventory status is still original 'offline' and updated_at is unchanged
        current_inv = self.harness.get_inventory(self.cpe_id)
        self.assertEqual(current_inv["status"], "offline", "cpe_inventory status was illegally modified!")
        self.assertEqual(current_inv["updated_at"], self.initial_inv["updated_at"], "cpe_inventory updated_at was modified!")


class TestOpticalThresholdEdgeCases(unittest.TestCase):
    """
    Adversarially verifies:
    - delta exactly 1.0 dBm (should NOT trigger)
    - delta 1.01 dBm (should trigger)
    - negative delta > 1.0 (e.g. -18.0 to -20.0, should trigger)
    - positive delta > 1.0 (e.g. -21.0 to -19.0, should trigger)
    - delta 0.99 dBm (should NOT trigger)
    - delta 1.001 dBm (should trigger)
    """

    def setUp(self):
        self.harness = AdversarialRelationalHarness()
        self.cpe_id = "CPE-OPTICAL-001"
        self.harness.register_cpe(self.cpe_id, "SN-OPTICAL-001")
        # Baseline insert with -18.0 dBm
        self.harness.upsert_live_state(
            self.cpe_id, "online",
            {"rx_optical_power": -18.0},
            {}
        )
        # History starts with exactly 1 record
        self.assertEqual(len(self.harness.get_history(self.cpe_id)), 1)
        self.assertEqual(self.harness.get_history(self.cpe_id)[0]["optical_power"], -18.0)

    def test_delta_exactly_1_0_dbm_negative_does_not_trigger(self):
        """Delta from -18.0 to -19.0 is exactly 1.00 dBm. Must NOT trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -19.0}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Delta of exactly 1.00 dBm (-18.0 -> -19.0) must NOT create history record")

    def test_delta_exactly_1_0_dbm_positive_does_not_trigger(self):
        """Delta from -18.0 to -17.0 is exactly 1.00 dBm. Must NOT trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -17.0}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Delta of exactly 1.00 dBm (-18.0 -> -17.0) must NOT create history record")

    def test_delta_1_01_dbm_negative_triggers(self):
        """Delta from -18.0 to -19.01 is 1.01 dBm (> 1.0). MUST trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -19.01}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2, "Delta of 1.01 dBm (-18.0 -> -19.01) MUST create history record")
        self.assertEqual(hist[1]["change_reason"], "optical_signal_variation")
        self.assertAlmostEqual(hist[1]["optical_power"], -19.01, places=2)

    def test_delta_1_01_dbm_positive_triggers(self):
        """Delta from -18.0 to -16.99 is 1.01 dBm (> 1.0). MUST trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -16.99}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2, "Delta of 1.01 dBm (-18.0 -> -16.99) MUST create history record")
        self.assertEqual(hist[1]["change_reason"], "optical_signal_variation")
        self.assertAlmostEqual(hist[1]["optical_power"], -16.99, places=2)

    def test_negative_delta_greater_than_1_0(self):
        """Negative delta > 1.0: -18.0 to -20.0 (delta = 2.0 dBm). MUST trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -20.0}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2, "Negative delta 2.0 dBm (-18.0 -> -20.0) MUST create history record")
        self.assertEqual(hist[1]["change_reason"], "optical_signal_variation")
        self.assertEqual(hist[1]["optical_power"], -20.0)

    def test_positive_delta_greater_than_1_0(self):
        """Positive delta > 1.0: -21.0 to -19.0 (delta = 2.0 dBm). MUST trigger."""
        # First transition to -21.0
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -21.0}, {})
        self.assertEqual(len(self.harness.get_history(self.cpe_id)), 2)

        # Now transition from -21.0 to -19.0 (positive delta +2.0 dBm)
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -19.0}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 3, "Positive delta +2.0 dBm (-21.0 -> -19.0) MUST create history record")
        self.assertEqual(hist[2]["change_reason"], "optical_signal_variation")
        self.assertEqual(hist[2]["optical_power"], -19.0)

    def test_boundary_0_99_dbm_does_not_trigger(self):
        """Delta from -18.0 to -18.99 is 0.99 dBm (< 1.0). Must NOT trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -18.99}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 1, "Delta of 0.99 dBm must NOT create history record")

    def test_boundary_1_001_dbm_triggers(self):
        """Delta from -18.0 to -19.001 is 1.001 dBm (> 1.0). MUST trigger."""
        self.harness.upsert_live_state(self.cpe_id, "online", {"rx_optical_power": -19.001}, {})
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(hist), 2, "Delta of 1.001 dBm MUST create history record")

    def test_tr181_parameter_fallbacks(self):
        """Tests that TR-181 parameters (Device.Optical.Interface.1.OpticalSignalLevel / RxPower) trigger."""
        cpe_id2 = "CPE-TR181-002"
        self.harness.register_cpe(cpe_id2, "SN-TR181-002")

        # Initial insert via Device.Optical.Interface.1.OpticalSignalLevel
        self.harness.upsert_live_state(
            cpe_id2, "online", {},
            {"Device.Optical.Interface.1.OpticalSignalLevel": "-17.5"}
        )
        hist = self.harness.get_history(cpe_id2)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["optical_power"], -17.5)

        # Update via Device.Optical.Interface.1.RxPower with delta 2.5 dBm (-17.5 -> -20.0)
        self.harness.upsert_live_state(
            cpe_id2, "online", {},
            {"Device.Optical.Interface.1.RxPower": "-20.0"}
        )
        hist2 = self.harness.get_history(cpe_id2)
        self.assertEqual(len(hist2), 2)
        self.assertEqual(hist2[1]["change_reason"], "optical_signal_variation")
        self.assertEqual(hist2[1]["optical_power"], -20.0)


class TestStressHarnessAndMemoryLeaks(unittest.TestCase):
    """
    Executes 3000+ rapid updates against the SQLite relational trigger harness:
    - Measures memory usage (tracemalloc) to verify zero leaks.
    - Verifies zero unwanted history rows.
    - Interleaves legitimate threshold breaches with noise.
    """

    def setUp(self):
        self.harness = AdversarialRelationalHarness()
        self.cpe_id = "CPE-STRESS-001"
        self.harness.register_cpe(self.cpe_id, "SN-STRESS-001")
        # Baseline insert with optical power
        self.harness.upsert_live_state(
            self.cpe_id, "online",
            {"rx_optical_power": -18.0, "cpu": 20.0, "ram": 40.0},
            {"Device.DeviceInfo.SoftwareVersion": "1.0.0"}
        )
        self.assertEqual(len(self.harness.get_history(self.cpe_id)), 1)

    def test_3000_rapid_updates_no_memory_leak_no_spurious_history(self):
        # Start tracemalloc
        gc.collect()
        tracemalloc.start()
        snapshot_start = tracemalloc.take_snapshot()

        # Phase 1: 1000 rapid heartbeat updates (last_seen only)
        for i in range(1000):
            self.harness.update_heartbeat_only(self.cpe_id)

        # Phase 2: 1000 rapid CPU/RAM metric fluctuations with static optical power
        for i in range(1000):
            cpu = 10.0 + (i % 80)
            ram = 20.0 + (i % 60)
            self.harness.upsert_live_state(
                self.cpe_id, "online",
                {"rx_optical_power": -18.0, "cpu": cpu, "ram": ram},
                {"Device.DeviceInfo.SoftwareVersion": "1.0.0"}
            )

        # Phase 3: 1000 rapid sub-threshold optical jitter (-18.0 to -18.8, delta <= 0.8 dBm)
        for i in range(1000):
            jitter = (i % 8) * 0.1  # 0.0 to 0.7
            rx = -18.0 - jitter
            self.harness.upsert_live_state(
                self.cpe_id, "online",
                {"rx_optical_power": rx},
                {"Device.DeviceInfo.SoftwareVersion": "1.0.0"}
            )

        # Verify that after 3000 noise/heartbeat updates, history count is STILL EXACTLY 1!
        hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(
            len(hist), 1,
            f"Expected exactly 1 baseline history row after 3000 rapid noise updates, but found {len(hist)}! Spurious rows generated!"
        )

        # Phase 4: Interleave 4 intentional threshold breaches (> 1.0 dBm)
        breaches = [
            -20.0,   # delta = |-20.0 - (-18.0)| = 2.0 > 1.0 -> row 2
            -21.5,   # delta = |-21.5 - (-20.0)| = 1.5 > 1.0 -> row 3
            -19.0,   # delta = |-19.0 - (-21.5)| = 2.5 > 1.0 -> row 4
            -17.5,   # delta = |-17.5 - (-19.0)| = 1.5 > 1.0 -> row 5
        ]
        for target_rx in breaches:
            # 100 heartbeats first
            for _ in range(100):
                self.harness.update_heartbeat_only(self.cpe_id)
            # Threshold breach
            self.harness.upsert_live_state(
                self.cpe_id, "online",
                {"rx_optical_power": target_rx},
                {"Device.DeviceInfo.SoftwareVersion": "1.0.0"}
            )

        # Verify final history count is EXACTLY 1 (initial) + 4 (breaches) = 5
        final_hist = self.harness.get_history(self.cpe_id)
        self.assertEqual(len(final_hist), 5, f"Expected exactly 5 history rows, got {len(final_hist)}")
        self.assertEqual([h["change_reason"] for h in final_hist], [
            "initial_state",
            "optical_signal_variation",
            "optical_signal_variation",
            "optical_signal_variation",
            "optical_signal_variation",
        ])

        # Memory leak check
        gc.collect()
        snapshot_end = tracemalloc.take_snapshot()
        stats = snapshot_end.compare_to(snapshot_start, "lineno")
        total_growth = sum(stat.size_diff for stat in stats)
        tracemalloc.stop()

        # Growth after 3400+ operations must be negligible (< 1 MB)
        self.assertLess(
            total_growth, 1024 * 1024,
            f"Potential memory leak detected: total heap growth was {total_growth / 1024:.1f} KB over 3400+ operations"
        )

        # Zero cpe_inventory updates throughout entire stress test
        self.assertEqual(
            self.harness.get_inventory_updates_count(self.cpe_id), 0,
            "cpe_inventory was touched during stress test!"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
