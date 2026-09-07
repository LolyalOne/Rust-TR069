#!/usr/bin/env python3
"""
Test Suite for PostgreSQL Hybrid Schema (Milestone 2)
Validates DDL syntax, table definitions, constraints, tablespaces,
and simulates trigger reconciliation semantics.

Supports:
  1. Offline static and lexical DDL validation of init.sql
  2. In-memory semantic simulation of relational integrity and triggers
  3. Optional live PostgreSQL integration tests (automatically enabled when PG is available)

Execution:
  python3 postgres/test_schema.py
  python3 -m unittest postgres/test_schema.py -v
"""

import os
import re
import sys
import copy
import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path


INIT_SQL_PATH = Path(__file__).resolve().parent / "init.sql"


class SQLDDLParser:
    """Parses and extracts structured schema components from init.sql."""

    def __init__(self, sql_content: str):
        self.raw_sql = sql_content
        self.cleaned_sql = self._strip_comments(sql_content)

    @staticmethod
    def _strip_comments(sql: str) -> str:
        lines = []
        for line in sql.splitlines():
            line_clean = re.sub(r"--.*$", "", line)
            lines.append(line_clean)
        return "\n".join(lines)

    def get_tablespace_block(self) -> str:
        match = re.search(r"DO\s+\$\$(.*?)\$\$;", self.raw_sql, re.DOTALL | re.IGNORECASE)
        return match.group(1) if match else ""

    def get_create_table_statements(self) -> dict[str, dict]:
        """Extracts CREATE [UNLOGGED] TABLE statements mapped by table name."""
        tables = {}
        pattern = re.compile(
            r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)(?:\s+TABLESPACE\s+(\w+))?;",
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(self.cleaned_sql):
            tbl_name = match.group(1).lower()
            tables[tbl_name] = {
                "body": match.group(2).strip(),
                "tablespace": match.group(3) or "",
                "is_unlogged": bool(re.search(r"CREATE\s+UNLOGGED\s+TABLE", match.group(0), re.IGNORECASE)),
                "full_sql": match.group(0),
            }
        return tables

    def get_indexes(self) -> list[dict[str, str]]:
        """Extracts index definitions."""
        pattern = re.compile(
            r"CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)(?:\s+USING\s+(\w+))?\s*\((.*?)\);",
            re.IGNORECASE,
        )
        indexes = []
        for match in pattern.finditer(self.cleaned_sql):
            indexes.append({
                "index_name": match.group(1),
                "table_name": match.group(2).lower(),
                "method": (match.group(3) or "btree").lower(),
                "columns": match.group(4).strip(),
                "full_sql": match.group(0),
            })
        return indexes

    def get_functions(self) -> dict[str, str]:
        """Extracts PL/pgSQL function definitions."""
        functions = {}
        pattern = re.compile(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+(\w+)\s*\(\)\s*RETURNS\s+TRIGGER\s+AS\s+\$\$(.*?)\$\$\s+LANGUAGE\s+plpgsql;",
            re.DOTALL | re.IGNORECASE,
        )
        for match in pattern.finditer(self.raw_sql):
            func_name = match.group(1)
            functions[func_name] = match.group(2).strip()
        return functions

    def get_triggers(self) -> list[dict[str, str]]:
        """Extracts trigger definitions."""
        triggers = []
        pattern = re.compile(
            r"CREATE\s+TRIGGER\s+(\w+)\s+(BEFORE|AFTER)\s+(.*?)\s+ON\s+(\w+)\s+FOR\s+EACH\s+ROW\s+EXECUTE\s+FUNCTION\s+(\w+)\(\);",
            re.IGNORECASE,
        )
        for match in pattern.finditer(self.cleaned_sql):
            triggers.append({
                "trigger_name": match.group(1),
                "timing": match.group(2).upper(),
                "events": match.group(3).upper(),
                "table_name": match.group(4).lower(),
                "function_name": match.group(5),
            })
        return triggers


class MockCpeDatabase:
    """
    In-memory relational and trigger semantic simulator for PostgreSQL TR-369 ACS schema.
    Faithfully simulates constraints, foreign keys, cascades, and PL/pgSQL reconciliation triggers.
    """

    def __init__(self):
        self.cpe_inventory: dict[str, dict] = {}
        self.cpe_live_state: dict[str, dict] = {}
        self.cpe_state_history: list[dict] = []
        self._history_id_seq = 1

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def insert_inventory(self, record: dict) -> dict:
        cpe_id = record.get("cpe_id")
        serial = record.get("serial_number")
        if not cpe_id or not serial:
            raise ValueError("cpe_id and serial_number are required")
        if cpe_id in self.cpe_inventory:
            raise ValueError(f"Primary key violation: cpe_id '{cpe_id}' already exists")
        for existing in self.cpe_inventory.values():
            if existing["serial_number"] == serial:
                raise ValueError(f"Unique constraint violation: serial_number '{serial}' already exists")

        now = self._now()
        row = {
            "cpe_id": cpe_id,
            "serial_number": serial,
            "manufacturer": record.get("manufacturer", ""),
            "model": record.get("model", ""),
            "oui": record.get("oui"),
            "product_class": record.get("product_class"),
            "hardware_version": record.get("hardware_version"),
            "software_version": record.get("software_version"),
            "description": record.get("description"),
            "status": record.get("status", "offline"),
            "created_at": record.get("created_at", now),
            "updated_at": record.get("updated_at", now),
        }
        self.cpe_inventory[cpe_id] = row
        return copy.deepcopy(row)

    def update_inventory(self, cpe_id: str, updates: dict) -> dict:
        if cpe_id not in self.cpe_inventory:
            raise KeyError(f"CPE not found: {cpe_id}")
        row = self.cpe_inventory[cpe_id]
        for k, v in updates.items():
            if k not in ("cpe_id", "created_at"):
                row[k] = v
        # Simulate trg_cpe_inventory_updated_at (fn_set_updated_at)
        row["updated_at"] = self._now()
        return copy.deepcopy(row)

    def delete_inventory(self, cpe_id: str) -> None:
        if cpe_id not in self.cpe_inventory:
            raise KeyError(f"CPE not found: {cpe_id}")
        del self.cpe_inventory[cpe_id]
        # Simulate ON DELETE CASCADE
        if cpe_id in self.cpe_live_state:
            del self.cpe_live_state[cpe_id]
        self.cpe_state_history = [h for h in self.cpe_state_history if h["cpe_id"] != cpe_id]

    def insert_live_state(self, record: dict) -> dict:
        cpe_id = record.get("cpe_id")
        if not cpe_id:
            raise ValueError("cpe_id is required for cpe_live_state")
        # Enforce Foreign Key to cpe_inventory
        if cpe_id not in self.cpe_inventory:
            raise ValueError(f"Foreign key violation: cpe_id '{cpe_id}' does not exist in cpe_inventory")
        if cpe_id in self.cpe_live_state:
            raise ValueError(f"Primary key violation: cpe_id '{cpe_id}' already in cpe_live_state")

        now = self._now()
        row = {
            "cpe_id": cpe_id,
            "endpoint_id": record.get("endpoint_id"),
            "current_parameters": copy.deepcopy(record.get("current_parameters", {})),
            "telemetry_metrics": copy.deepcopy(record.get("telemetry_metrics", {})),
            "status": record.get("status", "offline"),
            "ip_address": record.get("ip_address"),
            "firmware_version": record.get("firmware_version"),
            "last_seen": record.get("last_seen", now),
            "updated_at": record.get("updated_at", now),
        }
        self.cpe_live_state[cpe_id] = row
        # Trigger reconciliation
        self._trigger_reconcile(tg_op="INSERT", old_row=None, new_row=row)
        return copy.deepcopy(row)

    def update_live_state(self, cpe_id: str, updates: dict) -> dict:
        if cpe_id not in self.cpe_live_state:
            raise KeyError(f"CPE live state not found: {cpe_id}")
        old_row = copy.deepcopy(self.cpe_live_state[cpe_id])
        new_row = self.cpe_live_state[cpe_id]

        for k, v in updates.items():
            if k not in ("cpe_id",):
                new_row[k] = copy.deepcopy(v)

        # Simulate trg_cpe_live_state_updated_at (fn_set_updated_at)
        new_row["updated_at"] = self._now()

        # Trigger reconciliation
        self._trigger_reconcile(tg_op="UPDATE", old_row=old_row, new_row=new_row)
        return copy.deepcopy(new_row)

    def _trigger_reconcile(self, tg_op: str, old_row: dict | None, new_row: dict) -> None:
        """Faithfully mirrors fn_reconcile_cpe_live_state() in PL/pgSQL."""
        cpe_id = new_row["cpe_id"]
        # Step 1: Synchronize status and updated_at to cpe_inventory
        if cpe_id in self.cpe_inventory:
            self.cpe_inventory[cpe_id]["status"] = new_row["status"]
            self.cpe_inventory[cpe_id]["updated_at"] = new_row["updated_at"]

        # Step 2: Determine if history snapshot is warranted
        v_should_record = False
        v_reason = "telemetry_update"

        if tg_op == "INSERT":
            v_reason = "initial_state"
            v_should_record = True
        elif tg_op == "UPDATE" and old_row is not None:
            status_changed = old_row["status"] != new_row["status"]
            metrics_changed = old_row["telemetry_metrics"] != new_row["telemetry_metrics"]
            params_changed = old_row["current_parameters"] != new_row["current_parameters"]

            if status_changed and metrics_changed:
                v_reason = "status_and_metrics_changed"
                v_should_record = True
            elif status_changed:
                v_reason = "status_changed"
                v_should_record = True
            elif metrics_changed:
                v_reason = "telemetry_metrics_changed"
                v_should_record = True
            elif params_changed:
                v_reason = "parameters_changed"
                v_should_record = True

        # Step 3: Insert into cpe_state_history
        if v_should_record:
            history_entry = {
                "id": self._history_id_seq,
                "cpe_id": cpe_id,
                "status": new_row["status"],
                "current_parameters": copy.deepcopy(new_row["current_parameters"]),
                "telemetry_metrics": copy.deepcopy(new_row["telemetry_metrics"]),
                "recorded_at": new_row["updated_at"],
                "change_reason": v_reason,
            }
            self._history_id_seq += 1
            self.cpe_state_history.append(history_entry)

    def get_history(self, cpe_id: str) -> list[dict]:
        """Returns history snapshots ordered by recorded_at DESC."""
        records = [h for h in self.cpe_state_history if h["cpe_id"] == cpe_id]
        return sorted(records, key=lambda x: x["id"], reverse=True)


class TestPostgresSchemaDDL(unittest.TestCase):
    """Static and lexical DDL validation of init.sql."""

    @classmethod
    def setUpClass(cls):
        if not INIT_SQL_PATH.exists():
            raise FileNotFoundError(f"init.sql not found at {INIT_SQL_PATH}")
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            cls.raw_sql = f.read()
        cls.parser = SQLDDLParser(cls.raw_sql)
        cls.tables = cls.parser.get_create_table_statements()
        cls.indexes = cls.parser.get_indexes()
        cls.functions = cls.parser.get_functions()
        cls.triggers = cls.parser.get_triggers()

    def test_01_init_sql_file_exists_and_not_empty(self):
        self.assertGreater(len(self.raw_sql.strip()), 100)

    def test_02_ram_tablespace_creation(self):
        block = self.parser.get_tablespace_block()
        self.assertTrue(len(block) > 0, "Tablespace DO block not found in init.sql")
        self.assertIn("spcname = 'ram_tablespace'", block)
        self.assertIn("CREATE TABLESPACE ram_tablespace", block)
        self.assertIn("LOCATION '/var/lib/postgresql/ram_data'", block)

    def test_03_cpe_inventory_table_structure(self):
        self.assertIn("cpe_inventory", self.tables)
        tbl = self.tables["cpe_inventory"]
        self.assertFalse(tbl["is_unlogged"], "cpe_inventory must be a persistent (logged) table")
        body = tbl["body"]

        # Columns & constraints
        self.assertRegex(body, r"cpe_id\s+VARCHAR\(128\)\s+PRIMARY\s+KEY")
        self.assertRegex(body, r"serial_number\s+VARCHAR\(64\)\s+UNIQUE\s+NOT\s+NULL")
        self.assertRegex(body, r"manufacturer\s+VARCHAR\(64\)\s+NOT\s+NULL")
        self.assertRegex(body, r"model\s+VARCHAR\(64\)\s+NOT\s+NULL")
        self.assertRegex(body, r"oui\s+VARCHAR\(6\)")
        self.assertRegex(body, r"product_class\s+VARCHAR\(64\)")
        self.assertRegex(body, r"hardware_version\s+VARCHAR\(64\)")
        self.assertRegex(body, r"software_version\s+VARCHAR\(64\)")
        self.assertRegex(body, r"description\s+TEXT")
        self.assertRegex(body, r"status\s+VARCHAR\(32\)\s+NOT\s+NULL\s+DEFAULT\s+'offline'")
        self.assertRegex(body, r"created_at\s+TIMESTAMPTZ\s+(?:NOT\s+NULL\s+)?DEFAULT\s+CURRENT_TIMESTAMP")
        self.assertRegex(body, r"updated_at\s+TIMESTAMPTZ\s+(?:NOT\s+NULL\s+)?DEFAULT\s+CURRENT_TIMESTAMP")

    def test_04_cpe_live_state_table_structure(self):
        self.assertIn("cpe_live_state", self.tables)
        tbl = self.tables["cpe_live_state"]
        self.assertTrue(tbl["is_unlogged"], "cpe_live_state MUST be an UNLOGGED table")
        self.assertEqual(tbl["tablespace"], "ram_tablespace", "cpe_live_state must specify TABLESPACE ram_tablespace")
        body = tbl["body"]

        # Foreign Key and Columns
        self.assertRegex(body, r"cpe_id\s+VARCHAR\(128\)\s+PRIMARY\s+KEY\s+REFERENCES\s+cpe_inventory\(cpe_id\)\s+ON\s+DELETE\s+CASCADE")
        self.assertRegex(body, r"endpoint_id\s+VARCHAR\(256\)")
        self.assertRegex(body, r"current_parameters\s+JSONB\s+NOT\s+NULL\s+DEFAULT\s+'{}'::jsonb")
        self.assertRegex(body, r"telemetry_metrics\s+JSONB\s+NOT\s+NULL\s+DEFAULT\s+'{}'::jsonb")
        self.assertRegex(body, r"status\s+VARCHAR\(32\)\s+NOT\s+NULL\s+DEFAULT\s+'offline'")
        self.assertRegex(body, r"ip_address\s+VARCHAR\(64\)")
        self.assertRegex(body, r"firmware_version\s+VARCHAR\(64\)")
        self.assertRegex(body, r"last_seen\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+CURRENT_TIMESTAMP")
        self.assertRegex(body, r"updated_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+CURRENT_TIMESTAMP")

    def test_05_cpe_state_history_table_structure(self):
        self.assertIn("cpe_state_history", self.tables)
        tbl = self.tables["cpe_state_history"]
        self.assertFalse(tbl["is_unlogged"], "cpe_state_history must be a persistent (logged) table")
        body = tbl["body"]

        # Columns & Foreign Key
        self.assertRegex(body, r"id\s+BIGSERIAL\s+PRIMARY\s+KEY")
        self.assertRegex(body, r"cpe_id\s+VARCHAR\(128\)\s+NOT\s+NULL\s+REFERENCES\s+cpe_inventory\(cpe_id\)\s+ON\s+DELETE\s+CASCADE")
        self.assertRegex(body, r"status\s+VARCHAR\(32\)\s+NOT\s+NULL")
        self.assertRegex(body, r"current_parameters\s+JSONB\s+(?:NOT\s+NULL\s+)?DEFAULT\s+'{}'::jsonb")
        self.assertRegex(body, r"telemetry_metrics\s+JSONB\s+NOT\s+NULL\s+DEFAULT\s+'{}'::jsonb")
        self.assertRegex(body, r"recorded_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+CURRENT_TIMESTAMP")
        self.assertRegex(body, r"change_reason\s+VARCHAR\(64\)\s+NOT\s+NULL\s+DEFAULT\s+'telemetry_update'")

    def test_06_index_definitions(self):
        index_names = {idx["index_name"] for idx in self.indexes}
        self.assertIn("idx_cpe_live_params", index_names)
        self.assertIn("idx_cpe_live_telemetry", index_names)
        self.assertIn("idx_cpe_live_status", index_names)
        self.assertIn("idx_cpe_live_last_seen", index_names)
        self.assertIn("idx_cpe_history_lookup", index_names)
        self.assertIn("idx_cpe_history_telemetry", index_names)
        self.assertIn("idx_cpe_history_recorded_at", index_names)

        # Verify GIN indexes
        gin_indexes = [idx for idx in self.indexes if idx["method"] == "gin"]
        gin_indexed_cols = {idx["columns"].lower() for idx in gin_indexes}
        self.assertIn("current_parameters", gin_indexed_cols)
        self.assertIn("telemetry_metrics", gin_indexed_cols)

    def test_07_trigger_and_function_definitions(self):
        self.assertIn("fn_set_updated_at", self.functions)
        self.assertIn("fn_reconcile_cpe_live_state", self.functions)

        func_body = self.functions["fn_reconcile_cpe_live_state"]
        self.assertIn("UPDATE cpe_inventory", func_body)
        self.assertIn("status = NEW.status", func_body)
        self.assertIn("updated_at = NEW.updated_at", func_body)
        self.assertIn("INSERT INTO cpe_state_history", func_body)
        self.assertIn("status_and_metrics_changed", func_body)
        self.assertIn("status_changed", func_body)
        self.assertIn("telemetry_metrics_changed", func_body)
        self.assertIn("parameters_changed", func_body)

        trg_names = {t["trigger_name"] for t in self.triggers}
        self.assertIn("trg_cpe_inventory_updated_at", trg_names)
        self.assertIn("trg_cpe_live_state_reconcile", trg_names)

        reconcile_trg = next(t for t in self.triggers if t["trigger_name"] == "trg_cpe_live_state_reconcile")
        self.assertEqual(reconcile_trg["timing"], "AFTER")
        self.assertIn("INSERT", reconcile_trg["events"])
        self.assertIn("UPDATE", reconcile_trg["events"])
        self.assertEqual(reconcile_trg["table_name"], "cpe_live_state")
        self.assertEqual(reconcile_trg["function_name"], "fn_reconcile_cpe_live_state")

    def test_08_delimiter_and_dollar_quote_balance(self):
        """Verifies balanced dollar-quoting and block structure."""
        dollar_matches = re.findall(r"\$\$", self.raw_sql)
        self.assertEqual(len(dollar_matches) % 2, 0, "Mismatched $$ delimiter pairs in init.sql")
        # Ensure parenthesis balance in table bodies
        for name, data in self.tables.items():
            open_p = data["body"].count("(")
            close_p = data["body"].count(")")
            self.assertEqual(open_p, close_p, f"Mismatched parentheses in table '{name}'")


class TestTriggerReconciliationSemantics(unittest.TestCase):
    """Behavioral testing of state transitions and reconciliation triggers using simulated DB."""

    def setUp(self):
        self.db = MockCpeDatabase()
        self.cpe_data = {
            "cpe_id": "cpe-test-001",
            "serial_number": "SN-M2-123456",
            "manufacturer": "TP-Link",
            "model": "Archer-AX50",
            "oui": "00259E",
            "status": "offline",
        }

    def test_01_cpe_registration(self):
        row = self.db.insert_inventory(self.cpe_data)
        self.assertEqual(row["cpe_id"], "cpe-test-001")
        self.assertEqual(row["status"], "offline")
        self.assertIn("cpe-test-001", self.db.cpe_inventory)

    def test_02_unique_serial_number_enforced(self):
        self.db.insert_inventory(self.cpe_data)
        duplicate = copy.deepcopy(self.cpe_data)
        duplicate["cpe_id"] = "cpe-test-002"
        with self.assertRaises(ValueError) as ctx:
            self.db.insert_inventory(duplicate)
        self.assertIn("Unique constraint violation", str(ctx.exception))

    def test_03_live_state_requires_inventory_record(self):
        with self.assertRaises(ValueError) as ctx:
            self.db.insert_live_state({"cpe_id": "cpe-nonexistent", "status": "online"})
        self.assertIn("Foreign key violation", str(ctx.exception))

    def test_04_initial_telemetry_insert_reconciles_inventory_and_records_history(self):
        self.db.insert_inventory(self.cpe_data)

        # Initial live state insert
        live_record = {
            "cpe_id": "cpe-test-001",
            "endpoint_id": "proto::00259E-SN-M2-123456",
            "status": "online",
            "telemetry_metrics": {"cpu_usage": 42.5, "memory_usage": 60.0},
            "current_parameters": {"Device.DeviceInfo.SoftwareVersion": "1.0.0"},
        }
        self.db.insert_live_state(live_record)

        # Assert inventory updated
        inv = self.db.cpe_inventory["cpe-test-001"]
        self.assertEqual(inv["status"], "online")

        # Assert cpe_state_history has initial record
        self.assertEqual(len(self.db.cpe_state_history), 1)
        hist = self.db.cpe_state_history[0]
        self.assertEqual(hist["cpe_id"], "cpe-test-001")
        self.assertEqual(hist["status"], "online")
        self.assertEqual(hist["change_reason"], "initial_state")
        self.assertEqual(hist["telemetry_metrics"]["cpu_usage"], 42.5)

    def test_05_metric_alteration_creates_history_record(self):
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_live_state({
            "cpe_id": "cpe-test-001",
            "status": "online",
            "telemetry_metrics": {"cpu_usage": 42.5},
        })
        self.assertEqual(len(self.db.cpe_state_history), 1)

        # Update metrics (altering cpu_usage to 88.4)
        self.db.update_live_state("cpe-test-001", {
            "telemetry_metrics": {"cpu_usage": 88.4},
        })

        # History must contain 2 snapshots
        self.assertEqual(len(self.db.cpe_state_history), 2)
        h2 = self.db.cpe_state_history[1]
        self.assertEqual(h2["change_reason"], "telemetry_metrics_changed")
        self.assertEqual(h2["telemetry_metrics"]["cpu_usage"], 88.4)

    def test_06_status_transition_reconciles_inventory_and_records_history(self):
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_live_state({
            "cpe_id": "cpe-test-001",
            "status": "online",
            "telemetry_metrics": {"cpu_usage": 42.5},
        })

        # Device reboots
        self.db.update_live_state("cpe-test-001", {"status": "rebooting"})

        # Inventory status synced
        self.assertEqual(self.db.cpe_inventory["cpe-test-001"]["status"], "rebooting")

        # History updated
        self.assertEqual(len(self.db.cpe_state_history), 2)
        h2 = self.db.cpe_state_history[1]
        self.assertEqual(h2["change_reason"], "status_changed")
        self.assertEqual(h2["status"], "rebooting")

    def test_07_simultaneous_status_and_metric_change(self):
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_live_state({
            "cpe_id": "cpe-test-001",
            "status": "online",
            "telemetry_metrics": {"cpu_usage": 42.5},
        })

        self.db.update_live_state("cpe-test-001", {
            "status": "error",
            "telemetry_metrics": {"cpu_usage": 99.9, "error_flag": True},
        })

        self.assertEqual(self.db.cpe_inventory["cpe-test-001"]["status"], "error")
        self.assertEqual(len(self.db.cpe_state_history), 2)
        self.assertEqual(self.db.cpe_state_history[1]["change_reason"], "status_and_metrics_changed")

    def test_08_parameter_change_records_history(self):
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_live_state({
            "cpe_id": "cpe-test-001",
            "status": "online",
            "current_parameters": {"Device.WiFi.Radio.1.Status": "Down"},
        })

        self.db.update_live_state("cpe-test-001", {
            "current_parameters": {"Device.WiFi.Radio.1.Status": "Up"},
        })

        self.assertEqual(len(self.db.cpe_state_history), 2)
        self.assertEqual(self.db.cpe_state_history[1]["change_reason"], "parameters_changed")

    def test_09_heartbeat_last_seen_does_not_pollute_history(self):
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_live_state({
            "cpe_id": "cpe-test-001",
            "status": "online",
            "telemetry_metrics": {"cpu_usage": 42.5},
            "last_seen": "2026-09-07T00:00:00Z",
        })
        self.assertEqual(len(self.db.cpe_state_history), 1)

        # Heartbeat ping only modifies last_seen without metrics or status changes
        self.db.update_live_state("cpe-test-001", {
            "last_seen": "2026-09-07T00:00:30Z",
        })

        # History should still only have 1 record
        self.assertEqual(len(self.db.cpe_state_history), 1)

    def test_10_cascade_delete_removes_live_state_and_history(self):
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_live_state({
            "cpe_id": "cpe-test-001",
            "status": "online",
            "telemetry_metrics": {"cpu_usage": 42.5},
        })
        self.assertEqual(len(self.db.cpe_state_history), 1)

        # Delete inventory
        self.db.delete_inventory("cpe-test-001")

        self.assertNotIn("cpe-test-001", self.db.cpe_inventory)
        self.assertNotIn("cpe-test-001", self.db.cpe_live_state)
        self.assertEqual(len(self.db.cpe_state_history), 0)

    def test_11_multi_device_isolation(self):
        """Ensures state changes in device A do not bleed into device B."""
        cpe_b = {
            "cpe_id": "cpe-test-002",
            "serial_number": "SN-M2-654321",
            "manufacturer": "Huawei",
            "model": "OptiXstar",
            "status": "offline",
        }
        self.db.insert_inventory(self.cpe_data)
        self.db.insert_inventory(cpe_b)

        self.db.insert_live_state({"cpe_id": "cpe-test-001", "status": "online"})
        self.db.insert_live_state({"cpe_id": "cpe-test-002", "status": "offline"})

        self.db.update_live_state("cpe-test-001", {"telemetry_metrics": {"cpu": 50}})

        hist_a = self.db.get_history("cpe-test-001")
        hist_b = self.db.get_history("cpe-test-002")

        self.assertEqual(len(hist_a), 2)
        self.assertEqual(len(hist_b), 1)
        self.assertEqual(self.db.cpe_inventory["cpe-test-002"]["status"], "offline")


def check_live_postgres() -> bool:
    """Checks if a local or containerized PostgreSQL instance is reachable."""
    if not shutil.which("psql"):
        return False
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "acs_user")
    cmd = ["psql", "-h", host, "-p", port, "-U", user, "-d", "acs_db", "-c", "SELECT 1;"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        return res.returncode == 0
    except Exception:
        return False


class TestLivePostgresIntegration(unittest.TestCase):
    """Executes DDL statements directly against live PostgreSQL if available."""

    @unittest.skipUnless(check_live_postgres(), "Live PostgreSQL instance not reachable; skipping live DB test.")
    def test_live_postgres_ddl_execution(self):
        host = os.environ.get("POSTGRES_HOST", "localhost")
        port = os.environ.get("POSTGRES_PORT", "5432")
        user = os.environ.get("POSTGRES_USER", "acs_user")
        db = os.environ.get("POSTGRES_DB", "acs_db")

        cmd = ["psql", "-h", host, "-p", port, "-U", user, "-d", db, "-f", str(INIT_SQL_PATH)]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0, f"Failed executing init.sql on live Postgres: {res.stderr}")


def run_tests() -> int:
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    suite.addTests(loader.loadTestsFromTestCase(TestPostgresSchemaDDL))
    suite.addTests(loader.loadTestsFromTestCase(TestTriggerReconciliationSemantics))
    suite.addTests(loader.loadTestsFromTestCase(TestLivePostgresIntegration))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())
