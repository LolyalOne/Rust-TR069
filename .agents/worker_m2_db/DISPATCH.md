## 2026-09-07T01:22:13Z

You are the Database & Schema Worker for Milestone 2.
Your identity:
- Archetype: teamwork_preview_worker
- Working directory: /mnt/d/Projetos/TR069-181/.agents/worker_m2_db/
- Parent conversation ID: 6258e12c-9553-47a2-9624-69521a0b2d82
- Authoritative user request: /mnt/d/Projetos/TR069-181/ORIGINAL_REQUEST.md
- Scope reference: /mnt/d/Projetos/TR069-181/.agents/orchestrator_1/PROJECT.md
- Database Architecture Design: /mnt/d/Projetos/TR069-181/.agents/explorer_arch_1/handoff.md § 3.1
- Protocol Spec Findings: /mnt/d/Projetos/TR069-181/.agents/spec_miner_usp_1/handoff.md § 5.3

You MUST read /mnt/d/Projetos/TR069-181/ORIGINAL_REQUEST.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Exclusive file ownership:
You own exclusively:
- /mnt/d/Projetos/TR069-181/postgres/init.sql
- /mnt/d/Projetos/TR069-181/postgres/test_schema.py

Your tasks for Milestone 2:
1. Implement `/mnt/d/Projetos/TR069-181/postgres/init.sql`:
   - Tablespace creation on RAM tmpfs:
     ```sql
     DO $$
     BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') THEN
             CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
         END IF;
     END $$;
     ```
   - Persistent `cpe_inventory` table:
     `cpe_id` VARCHAR(128) PRIMARY KEY, `serial_number` VARCHAR(64) UNIQUE NOT NULL, `manufacturer` VARCHAR(64) NOT NULL, `model` VARCHAR(64) NOT NULL, `oui` VARCHAR(6), `product_class` VARCHAR(64), `hardware_version` VARCHAR(64), `software_version` VARCHAR(64), `description` TEXT, `status` VARCHAR(32) NOT NULL DEFAULT 'offline', `created_at` TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, `updated_at` TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP.
   - In-RAM unlogged table `cpe_live_state`:
     `CREATE UNLOGGED TABLE IF NOT EXISTS cpe_live_state (...) TABLESPACE ram_tablespace;`
     `cpe_id` VARCHAR(128) PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE, `endpoint_id` VARCHAR(256), `current_parameters` JSONB NOT NULL DEFAULT '{}'::jsonb, `telemetry_metrics` JSONB NOT NULL DEFAULT '{}'::jsonb, `status` VARCHAR(32) NOT NULL DEFAULT 'offline', `ip_address` VARCHAR(64), `firmware_version` VARCHAR(64), `last_seen` TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, `updated_at` TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP.
     GIN indexes on `current_parameters` and `telemetry_metrics`, B-tree on `status` and `last_seen`.
   - Persistent historical table `cpe_state_history`:
     `id` BIGSERIAL PRIMARY KEY, `cpe_id` VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE, `status` VARCHAR(32) NOT NULL, `current_parameters` JSONB DEFAULT '{}'::jsonb, `telemetry_metrics` JSONB NOT NULL DEFAULT '{}'::jsonb, `recorded_at` TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, `change_reason` VARCHAR(64) NOT NULL DEFAULT 'telemetry_update'.
     Indexes on `(cpe_id, recorded_at DESC)`, GIN on `telemetry_metrics`.
   - Reconciliation trigger function `fn_reconcile_cpe_live_state()` and trigger `trg_cpe_live_state_reconcile`:
     AFTER INSERT OR UPDATE ON `cpe_live_state`
     - Updates `cpe_inventory.status = NEW.status`, `updated_at = NEW.updated_at` where `cpe_id = NEW.cpe_id`.
     - Inserts row into `cpe_state_history` on INSERT or on UPDATE when status, telemetry_metrics, or current_parameters change, recording `change_reason`.
   - Helper trigger `trg_cpe_inventory_updated_at` updating `updated_at` on `cpe_inventory`.
2. Implement `/mnt/d/Projetos/TR069-181/postgres/test_schema.py`:
   - Standalone test script validating SQL DDL syntax, verifying table definitions, constraints, tablespace syntax, and trigger semantics (mocking or dry-run SQL parsing).
3. Run verification tests and document all commands and outputs in your report.

Write your report to:
/mnt/d/Projetos/TR069-181/.agents/worker_m2_db/handoff.md
Send a completion message back to parent when done.
