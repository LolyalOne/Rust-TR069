## 2026-09-07T06:00:56Z
You are explorer_m2_orch2_1 (teamwork_preview_explorer).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_1

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/DEAD_ENDS.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/auditor_m2_1/handoff.md (Full evidence report from Forensic Auditor)
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/test_schema.py
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/test_reconciliation_empirical.py

TASK:
Investigate and formulate the fix strategy for Milestone 2 (PostgreSQL):
1. Fix `postgres/init.sql` so that `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';` is a top-level command executed outside of any transactional `DO $$` block.
2. Fix the reconciliation trigger function `reconcile_live_to_history` (and table `cpe_historical_metrics` / `cpe_state_history`) so it triggers strictly when optical signal variation is > 1.0 dBm (comparing NEW.telemetry_metrics->>'rx_optical_power' or similar optical metrics with previous), inserting into `cpe_historical_metrics` WITHOUT performing an `UPDATE` on `cpe_inventory`.
3. Verify what updates `postgres/test_schema.py` and `postgres/test_reconciliation_empirical.py` require to test these exact conditions accurately and pass 100%.

Deliver a detailed, concrete fix plan in `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_1/handoff.md` and message the orchestrator when finished.
