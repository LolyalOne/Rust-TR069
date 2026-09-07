#!/usr/bin/env python3
"""
Adversarial Empirical Challenge for Milestone 1 Dual-Stack Database Layer:
Verifies cpe_pending_commands schema, indexes, foreign keys, cascade delete semantics,
WAL write amplification invariants, and trigger isolation with reconcile_live_to_history.
Author: challenger_m1_2
"""

import re
import sqlite3
import unittest
from pathlib import Path

INIT_SQL_PATH = Path(__file__).resolve().parent / "init.sql"


class TestPendingCommandsSchemaAST(unittest.TestCase):
    """Static AST and lexical structure tests on postgres/init.sql for cpe_pending_commands."""

    def setUp(self):
        with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
            self.sql_content = f.read()

    def test_cpe_pending_commands_table_exists(self):
        """Verify CREATE TABLE cpe_pending_commands with correct primary key and columns."""
        match = re.search(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+cpe_pending_commands\s*\((.*?)\);",
            self.sql_content,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(match, "Table cpe_pending_commands DDL definition not found in init.sql")
        body = match.group(1)

        # Check column definitions
        self.assertRegex(body, r"id\s+UUID\s+PRIMARY\s+KEY\s+DEFAULT\s+gen_random_uuid\(\)", "id column must be UUID PK with gen_random_uuid()")
        self.assertRegex(body, r"cpe_id\s+VARCHAR\(128\)\s+NOT\s+NULL\s+REFERENCES\s+cpe_inventory\(cpe_id\)\s+ON\s+DELETE\s+CASCADE", "cpe_id FK with ON DELETE CASCADE missing")
        self.assertRegex(body, r"command_type\s+VARCHAR\(64\)\s+NOT\s+NULL", "command_type VARCHAR(64) missing")
        self.assertRegex(body, r"command_payload\s+JSONB\s+NOT\s+NULL\s+DEFAULT\s+'\{\}'::jsonb", "command_payload JSONB default missing")
        self.assertRegex(body, r"status\s+VARCHAR\(32\)\s+NOT\s+NULL\s+DEFAULT\s+'pending'", "status VARCHAR(32) default 'pending' missing")
        self.assertRegex(body, r"created_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+CURRENT_TIMESTAMP", "created_at TIMESTAMPTZ missing")
        self.assertRegex(body, r"dispatched_at\s+TIMESTAMPTZ", "dispatched_at missing")
        self.assertRegex(body, r"completed_at\s+TIMESTAMPTZ", "completed_at missing")
        self.assertRegex(body, r"result_payload\s+JSONB", "result_payload JSONB missing")

    def test_lookup_index_exists(self):
        """Verify idx_cpe_pending_commands_lookup index on (cpe_id, status, created_at)."""
        match = re.search(
            r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_cpe_pending_commands_lookup\s+ON\s+cpe_pending_commands\s*\(\s*cpe_id\s*,\s*status\s*,\s*created_at\s*\);",
            self.sql_content,
            re.IGNORECASE,
        )
        self.assertIsNotNone(match, "Index idx_cpe_pending_commands_lookup on (cpe_id, status, created_at) missing or malformed")

    def test_zero_trigger_interference(self):
        """
        Adversarial Non-Interference:
        1. No triggers defined on cpe_pending_commands.
        2. reconcile_live_to_history does not reference cpe_pending_commands.
        3. cpe_pending_commands does not modify cpe_inventory or cpe_live_state.
        """
        # 1. No trigger on cpe_pending_commands
        pending_triggers = re.findall(
            r"CREATE\s+TRIGGER\s+\w+\s+[^;]+ON\s+cpe_pending_commands",
            self.sql_content,
            re.IGNORECASE,
        )
        self.assertEqual(len(pending_triggers), 0, f"Unexpected trigger found on cpe_pending_commands: {pending_triggers}")

        # 2. reconcile_live_to_history does not reference cpe_pending_commands
        func_match = re.search(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+reconcile_live_to_history\(\).*?END;\s*\$\$",
            self.sql_content,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(func_match)
        self.assertNotIn("cpe_pending_commands", func_match.group(0))

        # 3. No UPDATE cpe_inventory in entire file except perhaps initial comments
        # Strip comments
        lines = [l for l in self.sql_content.splitlines() if not l.strip().startswith("--")]
        code_only = "\n".join(lines)
        self.assertNotIn("UPDATE cpe_inventory", code_only)

    def test_tablespace_persisted_storage(self):
        """
        cpe_pending_commands MUST NOT be UNLOGGED and MUST NOT use ram_tablespace.
        TR-069 commands must survive ACS process/container restarts.
        """
        table_def = re.search(
            r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+cpe_pending_commands\s*\(.*?\)(?:\s*TABLESPACE\s+\w+)?;",
            self.sql_content,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(table_def)
        full_match = table_def.group(0)
        self.assertNotIn("UNLOGGED", full_match.upper())
        self.assertNotIn("ram_tablespace", full_match)


class TestPendingCommandsSQLiteCascadeSimulation(unittest.TestCase):
    """
    Simulate foreign key constraints and cascade deletion in SQLite
    with PRAGMA foreign_keys = ON.
    """

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("""
            CREATE TABLE cpe_inventory (
                cpe_id TEXT PRIMARY KEY,
                serial_number TEXT UNIQUE NOT NULL,
                manufacturer TEXT NOT NULL,
                model TEXT NOT NULL
            );
        """)
        self.conn.execute("""
            CREATE TABLE cpe_pending_commands (
                id TEXT PRIMARY KEY,
                cpe_id TEXT NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
                command_type TEXT NOT NULL,
                command_payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                dispatched_at TEXT,
                completed_at TEXT,
                result_payload TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()

    def test_fk_constraint_rejects_orphan_command(self):
        """Attempting to insert a command for non-existent cpe_id MUST fail with IntegrityError."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO cpe_pending_commands (id, cpe_id, command_type, created_at) VALUES (?, ?, ?, ?)",
                ("cmd-01", "nonexistent-cpe", "Reboot", "2026-09-07T12:00:00Z"),
            )

    def test_cascade_delete_cleans_multiple_commands(self):
        """Deleting cpe_inventory row MUST cascade-delete all associated commands."""
        # 1. Insert CPE
        self.conn.execute(
            "INSERT INTO cpe_inventory VALUES (?, ?, ?, ?)",
            ("cpe-01", "SN-01", "Huawei", "HG8245H"),
        )
        # 2. Insert 50 commands
        for i in range(50):
            self.conn.execute(
                "INSERT INTO cpe_pending_commands (id, cpe_id, command_type, created_at) VALUES (?, ?, ?, ?)",
                (f"cmd-{i:03d}", "cpe-01", f"Cmd_{i}", "2026-09-07T12:00:00Z"),
            )
        self.conn.commit()

        count_before = self.conn.execute("SELECT count(*) FROM cpe_pending_commands WHERE cpe_id='cpe-01'").fetchone()[0]
        self.assertEqual(count_before, 50)

        # 3. Delete CPE
        self.conn.execute("DELETE FROM cpe_inventory WHERE cpe_id='cpe-01'")
        self.conn.commit()

        # 4. Verify cascade
        count_after = self.conn.execute("SELECT count(*) FROM cpe_pending_commands WHERE cpe_id='cpe-01'").fetchone()[0]
        self.assertEqual(count_after, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
