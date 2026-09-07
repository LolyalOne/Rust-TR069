# Milestone 1 Empirical Challenge Report: Dual-Stack Infrastructure & Data Layer

**Agent**: `challenger_m1_1`  
**Roles**: Critic, Specialist (`doubt-driven-development`)  
**Target Repository**: `Rust-TR069`  
**Milestone**: Milestone 1 — Infra & Data Layer (Dual-Stack TR-069 / TR-369)  
**Date**: 2026-09-07T15:02:00Z  
**Verdict**: **REQUEST_CHANGES**  

---

## Challenge Summary

- **Overall Risk Assessment**: **HIGH**
- **Core Verdict**: **REQUEST_CHANGES**
- **Summary of Findings**:
  1. **CRITICAL DEFECT (V1)**: Silent Phantom Queueing Bug in `POST /api/v1/cpes/{cpe_id}/reboot`. An invalid `?protocol=` query parameter results in HTTP 200 OK claiming `status='queued'`, but the command is NEVER inserted into `cpe_pending_commands` nor published to MQTT. The operation silently drops.
  2. **HIGH DEFECT (V2)**: Unrestricted Status and State Machine Rewind in `PATCH /api/v1/cpes/{cpe_id}/commands/{command_id}`. Arbitrary string statuses (e.g. `"completely_bogus_status"`) are accepted and stored. Terminal commands (`completed`, `failed`) can be arbitrarily rewound to `pending`, creating duplicate execution hazards for the TR-069 worker.
  3. **MEDIUM FINDING (V3)**: `command_id` route parameter is typed as `str` rather than `UUID`. In PostgreSQL, non-UUID strings risk throwing unhandled `500 Internal Server Error` (`DataError`) instead of clean `422 Unprocessable Entity`.
  4. **PASS**: Docker Compose YAML validity, port mapping collision freedom (1883, 7547, 8000), memory limits (configure_limits.py), PostgreSQL AST schema/triggers (68 tests passing), oversized payloads (>64KB), deeply nested JSON (35 levels), SQL injection resilience, and concurrency.

---

## 1. Observation

### 1.1 Verbatim Defect 1: Phantom Queueing on Reboot (`POST /api/v1/cpes/{cpe_id}/reboot`)
- **File**: `python-api/app/routers/cpes.py`, lines 229–273:
```python
229:     proto = (protocol or "dual").lower()
230: 
231:     # If TR-069 or dual, enqueue in cpe_pending_commands
232:     pending_cmd = None
233:     if proto in ("tr069", "tr-069", "dual"):
234:         pending_cmd = CpePendingCommand(
235:             cpe_id=cpe_id,
236:             command_type="Reboot",
237:             command_payload={"command": "Reboot", "command_key": f"reboot-{cpe_id}"},
238:             status="pending",
239:         )
240:         db.add(pending_cmd)
241:         await db.commit()
242:         await db.refresh(pending_cmd)
243: 
244:     # If TR-369 or dual, publish to MQTT
245:     if proto in ("tr369", "tr-369", "dual"):
...
262:     else:
263:         # TR-069 only mode
264:         now = datetime.now(timezone.utc)
265:         return CommandDispatchResponse(
266:             status="queued",
267:             cpe_id=cpe_id,
268:             command="Reboot",
269:             command_key=str(pending_cmd.id) if pending_cmd else f"reboot-{cpe_id}",
270:             topic="tr069/cwmp",
271:             dispatched_at=now,
272:         )
```
- **Direct Empirical Test Result**:
```bash
2026-09-07 11:58:01,413 [INFO] httpx: HTTP Request: POST http://test/api/v1/cpes/cpe-test-proto/reboot?protocol=bogus_protocol "HTTP/1.1 200 OK"
Status code: 200
Response JSON: {'status': 'queued', 'cpe_id': 'cpe-test-proto', 'command': 'Reboot', 'command_key': 'reboot-cpe-test-proto', 'topic': 'tr069/cwmp', 'dispatched_at': '2026-09-07T14:58:01.410959Z'}
2026-09-07 11:58:01,431 [INFO] httpx: HTTP Request: GET http://test/api/v1/cpes/cpe-test-proto/commands "HTTP/1.1 200 OK"
Commands in queue: []
```
*Direct observation*: API returned HTTP 200 OK with `status="queued"`, but `Commands in queue` is `[]` (0 records). The command was dropped without any trace in the database or broker.

### 1.2 Verbatim Defect 2: Unrestricted Status and State Machine Rewind (`PATCH /api/v1/cpes/{cpe_id}/commands/{command_id}`)
- **Files**: `python-api/app/schemas.py:113-118`, `python-api/app/routers/cpes.py:390-396`:
```python
# schemas.py:
class PendingCommandUpdate(BaseModel):
    status: Optional[str] = Field(None, max_length=32, description="Execution status (e.g. dispatched, completed, failed)")
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_payload: Optional[dict[str, Any]] = None

# cpes.py:
    update_data = payload.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(cmd, k, v)

    await db.commit()
    await db.refresh(cmd)
    return cmd
```
- **Direct Empirical Test Result**:
```bash
Created command with status: pending
PATCH /commands/<id> with status='completely_bogus_status' -> HTTP 200 OK
Stored status in DB: 'completely_bogus_status'

PATCH /commands/<id> with status='completed' -> HTTP 200 OK
Stored status in DB: 'completed'

PATCH /commands/<id> with status='pending' (rewind) -> HTTP 200 OK
Stored status in DB: 'pending'
```

### 1.3 Verbatim Execution: Docker Compose & Limit Checks
```bash
$ python3 configure_limits.py --verify
==============================================================
  Verifying Resource Limits: docker-compose.yml
==============================================================
  Service              | Configured Limit   | Status         
--------------------------------------------------------------
  postgres             | 1.5G               | VALID
  mosquitto            | 500M               | VALID
  rust-core            | 500M               | VALID
  python-api           | 1G                 | VALID
==============================================================
  All memory limits are valid and correctly configured.
```
- Direct verification: PyYAML parse confirmed services `postgres`, `mosquitto`, `rust-core`, `python-api`.
- Host ports mapped: `1883` (mosquitto), `7547` (rust-core), `8000` (python-api). Zero port collisions.

### 1.4 Verbatim Execution: PostgreSQL Schema Suite
```bash
$ python3 -m unittest discover -s postgres -p "test_*.py" -v
...
Ran 68 tests in 2.054s
OK (skipped=1)
```

### 1.5 Verbatim Execution: Empirical Stress Test Suite (`test_challenger_m1.py`)
```bash
$ PYTHONPATH=python-api pytest python-api/tests/test_challenger_m1.py -v
============================== 8 passed in 3.86s ===============================
```

---

## 2. Logic Chain

1. **Analysis of Reboot Endpoint Logic (`Finding 1`)**:
   - The endpoint expects `protocol` to indicate dispatch target: `tr069`, `tr369`, or `dual`.
   - Lines 233 and 245 check specific strings using `proto in (...)`.
   - Line 262 executes an unconditional `else:` when `proto` is neither `tr369` nor `dual`. The author intended this `else:` for `proto == 'tr069'`.
   - However, because there is no prior validation on `protocol`, ANY unknown or misspelled string (e.g. `?protocol=tr069_ont`, `?protocol=legacy`, `?protocol=bogus`) enters line 262.
   - Because `pending_cmd` was only initialized in line 233 (`if proto in ('tr069', 'tr-069', 'dual')`), for unknown protocols `pending_cmd` is `None`.
   - The endpoint still returns HTTP 200 with `status="queued"`, creating a silent phantom queueing defect where the caller is misled and no action is performed.

2. **Analysis of Status Lifecycle & Transition Logic (`Finding 2`)**:
   - TR-069 CWMP command lifecycle defined in `PROJECT.md` is strictly: `pending` -> `dispatched` -> `completed` (or `failed`).
   - `PendingCommandUpdate` defines `status: Optional[str] = Field(None, max_length=32)`. Any string up to 32 characters is accepted by Pydantic.
   - `update_cpe_command` blindly writes `setattr(cmd, k, v)` without validating the value against an enum or checking current status state.
   - In Milestone 3, `rust-core` polls commands using:
     `SELECT ... FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`
   - If an external client or buggy service patches a `completed` or `failed` command back to `pending`, `rust-core` will re-dispatch the command to the ONT.
   - For irreversible commands (such as `Reboot` or factory resets), rewinding status triggers duplicate reboots and severe disruption to customer premise equipment.

3. **Analysis of UUID Handling in Route Parameters (`Finding 3`)**:
   - In `get_cpe_command` and `update_cpe_command`, `command_id: str` is used.
   - While SQLite allows string comparison against a `VARCHAR(36)` column, PostgreSQL enforces strong typing for `UUID` columns.
   - Submitting an invalid UUID string to PostgreSQL via asyncpg causes `DataError: invalid input syntax for type uuid`, resulting in HTTP 500.
   - Defining `command_id: UUID` in FastAPI leverages Pydantic type coercion at the HTTP boundary, instantly returning HTTP 422 for malformed UUIDs before reaching PostgreSQL.

---

## 3. Caveats

- **Live PostgreSQL Network Execution**: The current testing environment runs unit tests and stress harnesses against SQLite in-memory (`sqlite+aiosqlite:///:memory:`) with AST validation of `postgres/init.sql`. The behavior of asyncpg with non-UUID path parameters was inferred from SQLAlchemy and asyncpg type contract specifications.
- **Milestone 2 & 3 Dependencies**: The Axum HTTP server and SOAP XML parser in `rust-core` are planned for Milestones 2 and 3; tests here evaluated the API boundary and data contract that those milestones will consume.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

While the foundational infrastructure (`docker-compose.yml`, memory allocations, PostgreSQL DDL, and cascade cleanup) is robust and passes all stress and AST tests, two significant logic defects in `python-api` must be addressed before proceeding to Milestone 2:

### Required Action Items for Worker:

1. **Fix Reboot Protocol Validation**:
   - In `python-api/app/routers/cpes.py:reboot_cpe`, validate `protocol` upfront.
   - If `protocol` is not in `("tr069", "tr-069", "tr369", "tr-369", "dual")`, immediately raise `HTTPException(status_code=400, detail="Invalid protocol. Must be 'tr069', 'tr369', or 'dual'.")`.
   - Ensure the TR-069 only branch explicitly checks `proto in ("tr069", "tr-069")`.

2. **Enforce Status Enum and Transition Rules**:
   - In `python-api/app/schemas.py`, define a `CommandStatus` Literal or Enum: `Literal["pending", "dispatched", "completed", "failed"]`.
   - In `python-api/app/routers/cpes.py:update_cpe_command`, prohibit rewinding terminal states (`completed`, `failed`) back to `pending` or `dispatched`.

3. **Type `command_id` as `UUID` in Endpoints**:
   - In `get_cpe_command` and `update_cpe_command`, declare `command_id: UUID` so malformed UUID strings return HTTP 422 automatically.

---

## 5. Verification Method

To independently verify these findings and confirm subsequent fixes:

1. **Run Empirical Challenger Suite**:
   ```bash
   PYTHONPATH=python-api pytest python-api/tests/test_challenger_m1.py -v
   ```
   *Expected Current Output*: 8 passed in ~3.8s (including `test_reboot_endpoint_phantom_queue_vulnerability` which confirms the defect).

2. **Verify Docker Compose & Resource Limits**:
   ```bash
   python3 configure_limits.py --test && python3 configure_limits.py --verify
   ```
   *Expected Output*: 10 passed, all 4 services VALID, exit code 0.

3. **Verify PostgreSQL Schema & Trigger AST**:
   ```bash
   python3 -m unittest discover -s postgres -p "test_*.py" -v
   ```
   *Expected Output*: Ran 68 tests, OK (skipped=1).

4. **Run Baseline API Tests**:
   ```bash
   PYTHONPATH=python-api pytest python-api/tests/test_api.py -v
   ```
   *Expected Output*: 20 passed.
