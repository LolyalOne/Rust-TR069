# Dispatch for worker_m1_remediation

## Mission: Milestone 1 Remediation (Quality Gate Fixes)
Implement the required fixes identified by `reviewer_m1_1` and `challenger_m1_1` during Gate 1 review of `python-api`.

## Mandatory Inputs
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_4/PROJECT.md`
- Reviewer Report: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/reviewer_m1_1/handoff.md`
- Challenger Report: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/challenger_m1_1/handoff.md`

## Required Fixes
1. **Protocol Validation in Reboot Endpoint (`python-api/app/routers/cpes.py`)**:
   - In `POST /api/v1/cpes/{cpe_id}/reboot`, validate the `protocol` query parameter.
   - Allowed values: `"tr069"`, `"tr369"`, `"dual"` (case-insensitive, default `"dual"`).
   - If `protocol` is not one of the allowed values, raise `HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid protocol. Allowed: 'tr069', 'tr369', 'dual'")`.
   - Prevent phantom queueing (never return HTTP 200 with status="queued" on invalid protocol).
2. **UUID Route Parameter Typing (`python-api/app/routers/cpes.py`)**:
   - In `GET /api/v1/cpes/{cpe_id}/commands/{command_id}` and `PATCH /api/v1/cpes/{cpe_id}/commands/{command_id}`, type `command_id: UUID` (from `uuid import UUID`) instead of `str`.
   - In SQLAlchemy queries: use `str(command_id)` when querying with `UUID_TYPE`.
   - FastAPI automatically validates the UUID format and returns HTTP 422 for malformed strings, preventing PostgreSQL 500 DataError.
3. **Status Enum & Transition Guard (`python-api/app/routers/cpes.py`, `python-api/app/schemas.py`)**:
   - In `schemas.py`: Define `PendingCommandStatus(str, Enum)` with values `"pending"`, `"dispatched"`, `"completed"`, `"failed"`.
   - In `PendingCommandUpdate`: type `status: Optional[PendingCommandStatus] = None`.
   - In `PATCH /api/v1/cpes/{cpe_id}/commands/{command_id}`: enforce state transition validation:
     - Disallow rewinding from terminal states (`completed`, `failed`) back to `pending` or `dispatched`.
     - Return HTTP 400 with clear error message if an invalid state transition is attempted.
4. **Atomicity Guard on Dual Mode Reboot**:
   - In `POST /api/v1/cpes/{cpe_id}/reboot` under dual mode:
     - If MQTT publish raises an exception, ensure the database transaction is rolled back or the enqueued command is deleted so no orphan command remains before raising HTTP 503.
5. **Testing**:
   - Run all existing unit and adversarial test suites:
     - `PYTHONPATH=python-api pytest python-api/tests/ -v`
   - Ensure all tests pass 100% and add test cases verifying:
     - `protocol=invalid` returns HTTP 400.
     - Malformed UUID returns HTTP 422.
     - State machine transition rewind returns HTTP 400.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write your report to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_remediation/handoff.md`.

## 2026-09-07T15:02:57Z
You are worker_m1_remediation.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_remediation

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Mandatory initial reads:
1. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md
2. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_remediation/DISPATCH.md
3. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_4/PROJECT.md
4. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/reviewer_m1_1/handoff.md
5. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/challenger_m1_1/handoff.md

Remediation Tasks:
1. Protocol Validation:
   - In python-api/app/routers/cpes.py reboot_cpe endpoint, validate protocol parameter against {"tr069", "tr369", "dual"}. Raise HTTPException(400) if invalid to prevent phantom queueing.
2. Route Parameter UUID Typing:
   - In get_cpe_command and update_cpe_command, type command_id: UUID.
   - When querying DB, use str(command_id). Malformed UUIDs will now cleanly return HTTP 422.
3. Status Enum & State Machine Transition Guard:
   - In schemas.py, define PendingCommandStatus Enum.
   - In update_cpe_command, disallow rewinding from terminal states (completed, failed). Raise HTTPException(400) on invalid transitions.
4. Atomicity on Dual Reboot Failure:
   - If MQTT publish fails in dual mode, remove the queued command from DB before raising HTTP 503.
5. Verification:
   - Run PYTHONPATH=python-api pytest python-api/tests/ -v
   - Ensure all unit and adversarial tests pass with zero failures.

Write your report to /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_remediation/handoff.md and send a completion message back.

