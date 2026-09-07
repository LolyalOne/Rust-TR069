#!/usr/bin/env python3
"""
Verification script for explorer_m3_3:
1. Validates SQL UPSERT query against postgres/init.sql schema for cpe_live_state
2. Verifies JSONB merge (||) semantics matching simulate_flow.sh Step 2 & Step 4
3. Verifies optical delta calculation for trigger reconciliation
"""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path("/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069")
INIT_SQL_PATH = REPO_ROOT / "postgres" / "init.sql"


class TestSqlUpsertVerification(unittest.TestCase):

    def test_schema_columns_match(self):
        """Verify that designed SQL query targets all columns in cpe_live_state in init.sql."""
        sql = INIT_SQL_PATH.read_text(encoding="utf-8")
        match = re.search(r"CREATE\s+UNLOGGED\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?cpe_live_state\s*\((.*?)\)\s*TABLESPACE", sql, re.DOTALL | re.IGNORECASE)
        self.assertIsNotNone(match, "cpe_live_state definition not found in init.sql")
        
        table_body = match.group(1)
        expected_columns = [
            "cpe_id",
            "endpoint_id",
            "current_parameters",
            "telemetry_metrics",
            "status",
            "ip_address",
            "firmware_version",
            "last_seen",
            "updated_at",
        ]
        for col in expected_columns:
            self.assertIn(col, table_body, f"Column {col} missing from cpe_live_state in init.sql")

    def test_upsert_sql_syntax_and_placeholders(self):
        """Verify the designed UPSERT SQL query binding placeholders and conflict handling."""
        upsert_sql = """
        INSERT INTO cpe_live_state (
            cpe_id,
            endpoint_id,
            current_parameters,
            telemetry_metrics,
            status,
            ip_address,
            firmware_version,
            last_seen,
            updated_at
        ) VALUES (
            $1,
            $2,
            COALESCE($3, '{}'::jsonb),
            COALESCE($4, '{}'::jsonb),
            COALESCE($5, 'online'),
            $6,
            $7,
            COALESCE($8, CURRENT_TIMESTAMP),
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (cpe_id) DO UPDATE SET
            endpoint_id = COALESCE(EXCLUDED.endpoint_id, cpe_live_state.endpoint_id),
            current_parameters = cpe_live_state.current_parameters || COALESCE(EXCLUDED.current_parameters, '{}'::jsonb),
            telemetry_metrics = cpe_live_state.telemetry_metrics || COALESCE(EXCLUDED.telemetry_metrics, '{}'::jsonb),
            status = COALESCE(EXCLUDED.status, cpe_live_state.status),
            ip_address = COALESCE(EXCLUDED.ip_address, cpe_live_state.ip_address),
            firmware_version = COALESCE(EXCLUDED.firmware_version, cpe_live_state.firmware_version),
            last_seen = COALESCE(EXCLUDED.last_seen, cpe_live_state.last_seen),
            updated_at = CURRENT_TIMESTAMP;
        """
        # Count $ parameters: $1 to $8
        for i in range(1, 9):
            self.assertIn(f"${i}", upsert_sql, f"Missing placeholder ${i}")
        self.assertNotIn("$9", upsert_sql, "Unexpected placeholder $9 found")
        self.assertIn("ON CONFLICT (cpe_id) DO UPDATE SET", upsert_sql)
        self.assertIn("cpe_live_state.current_parameters ||", upsert_sql)
        self.assertIn("cpe_live_state.telemetry_metrics ||", upsert_sql)

    def test_jsonb_merge_simulate_flow_steps(self):
        """Simulate JSONB concatenation (||) across simulate_flow.sh steps 2 and 4."""
        # Step 2: Initial telemetry
        step2_metrics = {
            "rx_optical_power": -18.5,
            "cpu_usage": 42.5,
            "memory_usage": 68.0,
            "rx_bytes": 1048576,
            "tx_bytes": 524288,
            "temperature": 45.2,
        }
        step2_params = {
            "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
            "Device.WiFi.Radio.1.Status": "Up",
        }

        # Step 4: Altered telemetry
        step4_metrics = {
            "rx_optical_power": -21.0,
            "cpu_usage": 88.4,
            "memory_usage": 75.2,
            "rx_bytes": 2097152,
            "tx_bytes": 1048576,
            "temperature": 52.8,
        }
        step4_params = {
            "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
            "Device.WiFi.Radio.1.Status": "Up",
        }

        # Emulate Postgres JSONB concatenation: left || right
        merged_metrics = dict(step2_metrics)
        merged_metrics.update(step4_metrics)

        merged_params = dict(step2_params)
        merged_params.update(step4_params)

        self.assertEqual(merged_metrics["rx_optical_power"], -21.0)
        self.assertEqual(merged_metrics["cpu_usage"], 88.4)
        self.assertEqual(merged_metrics["memory_usage"], 75.2)

        # Trigger delta calculation
        old_power = step2_metrics["rx_optical_power"]
        new_power = merged_metrics["rx_optical_power"]
        delta = abs(new_power - old_power)
        self.assertGreater(delta, 1.0, "Delta between -18.5 and -21.0 must exceed 1.0 dBm trigger threshold")
        self.assertAlmostEqual(delta, 2.5, places=2)


if __name__ == "__main__":
    unittest.main()
