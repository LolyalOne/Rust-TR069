# Dispatch Record: worker_m1_dualstack

## Mission: Milestone 1 — Infra & Data Layer (Docker Compose, PostgreSQL Queue, Python API)

### Mandatory Inputs
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (specifically `## Follow-up — 2026-09-07T14:20:33Z`)
- Scope & Architecture: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_4/PROJECT.md`
- Investigation Findings:
  - `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_api_db_1/handoff.md`
  - `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md`

### Write Ownership (Exclusive)
You own and may modify ONLY:
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/models.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/schemas.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/routers/cpes.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/tests/` (if adding unit tests)

### Implementation Tasks
1. **Docker Compose (`docker-compose.yml`)**:
   - In `rust-core` service:
     - Add `ports: - "7547:7547"`.
     - Add environment variables `CWMP_PORT: 7547` and `CWMP_HOST: "0.0.0.0"`.
   - Ensure `configure_limits.py` compatibility and that other services remain unchanged.
2. **PostgreSQL Schema (`postgres/init.sql`)**:
   - Add table `cpe_pending_commands` at the bottom of `init.sql`:
     ```sql
     CREATE TABLE IF NOT EXISTS cpe_pending_commands (
         id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
         command_type VARCHAR(64) NOT NULL,
         command_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
         status VARCHAR(32) NOT NULL DEFAULT 'pending',
         created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
         dispatched_at TIMESTAMPTZ,
         completed_at TIMESTAMPTZ,
         result_payload JSONB
     );
     CREATE INDEX IF NOT EXISTS idx_cpe_pending_commands_lookup
     ON cpe_pending_commands (cpe_id, status, created_at);
     ```
   - STRICT CONSTRAINT: Do NOT wrap `CREATE TABLESPACE` in a transaction block. Do NOT alter the optical power trigger or add any `UPDATE` statements to `cpe_inventory`.
3. **Python FastAPI Manager (`python-api/`)**:
   - In `app/models.py`:
     - Add `CpePendingCommand` model with UUID primary key (supporting SQLite in tests: `UUID_TYPE = Uuid().with_variant(String(36), "sqlite")` or `PG_UUID = String(36)`), `cpe_id`, `command_type`, `command_payload`, `status`, `created_at`, `dispatched_at`, `completed_at`, `result_payload`.
   - In `app/schemas.py`:
     - Add Pydantic schemas: `PendingCommandCreate`, `PendingCommandResponse`, `PendingCommandUpdate`.
   - In `app/routers/cpes.py`:
     - Endpoint `POST /api/v1/cpes/{cpe_id}/commands` to enqueue pending commands (`Reboot`, `GetParameterValues`).
     - Endpoint `GET /api/v1/cpes/{cpe_id}/commands` to view pending/history commands.
     - Update `POST /api/v1/cpes/{cpe_id}/reboot`: if device is TR-069 or MQTT publish fails/optional query param, enqueue in `cpe_pending_commands` while retaining full backward compatibility with TR-369 MQTT flow.
4. **Verification**:
   - Run PostgreSQL unit tests: `python3 -m unittest discover -s postgres -p "test_*.py" -v`
   - Run Python API pytest tests: `pytest python-api/tests/` (ensure 100% passing)

### Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

### Output
Write your comprehensive report to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_dualstack/handoff.md`.

## 2026-09-07T14:31:31Z
You are worker_m1_dualstack.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_dualstack

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Mandatory initial reads:
1. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md (specifically ## Follow-up — 2026-09-07T14:20:33Z)
2. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_dualstack/DISPATCH.md
3. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_4/PROJECT.md
4. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_api_db_1/handoff.md


## 2026-09-07T14:50:19Z
**Context**: Milestone 1 Implementation Monitoring
**Content**: Checking in on your progress with Step 3 (python-api models, schemas, and routers) and verification test suites. Please update your progress.md with your latest timestamp and status.
**Action**: Report current status and estimated time to completion or any blocking issue.
