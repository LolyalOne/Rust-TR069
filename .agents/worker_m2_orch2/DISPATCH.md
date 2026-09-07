## 2026-09-07T06:05:00Z

You are worker_m2_orch2 (teamwork_preview_worker).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m2_orch2

DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_2/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_3/handoff.md

EXCLUSIVE FILE OWNERSHIP:
You own and will modify:
1. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
2. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/test_schema.py
3. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/test_reconciliation_empirical.py
4. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh (specifically lines 516 & 598 to add "rx_optical_power" payloads)

IMPLEMENTATION REQUIREMENTS:
1. `postgres/init.sql`:
   - Place `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';` at the top level outside any transactional block (NO `DO $$ ... $$`).
   - Define primary table `cpe_historical_metrics` (with columns `id`, `cpe_id`, `status`, `current_parameters`, `telemetry_metrics`, `optical_power`, `recorded_at`, `change_reason`).
   - Define backward-compatibility view: `CREATE OR REPLACE VIEW cpe_state_history AS SELECT id, cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason FROM cpe_historical_metrics;`.
   - Implement trigger function `reconcile_live_to_history()` and trigger `reconcile_live_to_history` on `cpe_live_state`:
     * Strictly trigger when optical signal variation is > 1.0 dBm (`|NEW - OLD| > 1.0 dBm`), reading `NEW.telemetry_metrics->>'rx_optical_power'` (with fallbacks).
     * On INSERT: record baseline snapshot if optical power is present.
     * On UPDATE: record snapshot if optical delta > 1.0 dBm. Sub-1.0 dBm variations, CPU/RAM fluctuations, and routine heartbeats must NOT insert records.
     * NEVER perform `UPDATE cpe_inventory` (zero WAL write amplification).
     * Provide alias function `fn_reconcile_cpe_live_state()` for backward compatibility.
2. `postgres/test_schema.py`:
   - Update `test_02_ram_tablespace_creation` to verify top-level `CREATE TABLESPACE` and assert absence of `DO $$`.
   - Update table assertions for `cpe_historical_metrics` and view `cpe_state_history`.
   - Update trigger assertions: assert `UPDATE cpe_inventory` is NOT in `reconcile_live_to_history`, assert `rx_optical_power` and `1.0` are present.
   - Update `MockCpeDatabase` and tests to validate optical variation > 1.0 dBm and zero updates to `cpe_inventory`.
3. `postgres/test_reconciliation_empirical.py`:
   - Update SQLite harness to mirror `cpe_historical_metrics`, view `cpe_state_history`, and optical delta > 1.0 dBm without `cpe_inventory` updates.
   - Validate 100% tests passing standalone with `python3 postgres/test_reconciliation_empirical.py`.
4. `simulate_flow.sh`:
   - In Step 2 payload (line ~516), add `"rx_optical_power": -18.5`.
   - In Step 4 payload (line ~598), add `"rx_optical_power": -21.0`.
   - Validate shell syntax with `bash -n simulate_flow.sh`.

VERIFICATION:
You must execute:
- `python3 postgres/test_schema.py` (all tests pass)
- `python3 postgres/test_reconciliation_empirical.py` (all tests pass)
- `bash -n simulate_flow.sh` (valid syntax)

Write your full handoff report to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m2_orch2/handoff.md` and message the orchestrator with the results.
