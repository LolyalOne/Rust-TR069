# Milestone 1 Quality Gate Remediation Report

**Agent**: `worker_m1_remediation` (Roles: Implementer, QA, Specialist)  
**Target Repository**: `Rust-TR069`  
**Milestone**: Milestone 1 Remediation (Infra & Data Layer Quality Gate Fixes)  
**Date**: 2026-09-07T15:16:00Z  
**Verdict**: **RESOLVED / PASS**

---

## 1. Observation

Direct observations before and after remediation:

### 1.1 Remediation Task 1: Protocol Validation in `POST /api/v1/cpes/{cpe_id}/reboot`
- **Prior Defect**: An invalid or misspelled `protocol` query string (e.g. `?protocol=bogus` or `?protocol=snmp`) entered line 262's `else:` branch, returning HTTP 200 OK with `status="queued"` despite enqueuing zero rows in `cpe_pending_commands` and dispatching nothing to MQTT (phantom queueing).
- **Remediation**:
  - File: `python-api/app/routers/cpes.py:229-236`:
    ```python
    raw_proto = protocol or "dual"
    proto = raw_proto.lower().replace("-", "")
    if proto not in ("tr069", "tr369", "dual"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid protocol '{protocol}'. Allowed: 'tr069', 'tr369', 'dual'",
        )
    ```
- **Observed Result**: Calling `POST /api/v1/cpes/{cpe_id}/reboot?protocol=invalid_protocol` immediately returns `HTTP 400 Bad Request` with `{"detail": "Invalid protocol 'invalid_protocol'. Allowed: 'tr069', 'tr369', 'dual'"}`, preventing phantom queueing.

### 1.2 Remediation Task 2: Route Parameter UUID Typing in Command Endpoints
- **Prior Defect**: `get_cpe_command` and `update_cpe_command` declared `command_id: str`. In PostgreSQL, `CpePendingCommand.id == command_id` compiles to `cpe_pending_commands.id = %(id)s::UUID`, raising a `DataError: invalid input syntax for type uuid` (HTTP 500) on malformed UUID strings.
- **Remediation**:
  - File: `python-api/app/routers/cpes.py:356-370, 381-395`:
    ```python
    @router.get("/{cpe_id}/commands/{command_id}", response_model=PendingCommandResponse)
    async def get_cpe_command(
        cpe_id: str,
        command_id: UUID,
        db: AsyncSession = Depends(get_db),
    ): ...
        stmt = select(CpePendingCommand).where(
            CpePendingCommand.cpe_id == cpe_id,
            CpePendingCommand.id == str(command_id),
        )
    ```
- **Observed Result**: Passing a non-UUID string (`/commands/not-a-uuid`, `/commands/12345`, `/commands/' OR '1'='1`) is intercepted at the FastAPI route boundary by Pydantic UUID validation and cleanly returns `HTTP 422 Unprocessable Entity` before reaching SQL compilation.

### 1.3 Remediation Task 3: Status Enum & State Machine Transition Guard
- **Prior Defect**: `PendingCommandUpdate.status` accepted arbitrary strings up to 32 characters, and `PATCH /commands/{command_id}` allowed rewinding terminal commands (`completed`, `failed`) back to `pending` or `dispatched`, risking duplicate command dispatch by the TR-069 worker.
- **Remediation**:
  - File: `python-api/app/schemas.py:108-124`:
    ```python
    class PendingCommandStatus(str, Enum):
        PENDING = "pending"
        DISPATCHED = "dispatched"
        COMPLETED = "completed"
        FAILED = "failed"

        def __str__(self) -> str:
            return str(self.value)

    class PendingCommandUpdate(BaseModel):
        status: Optional[PendingCommandStatus] = Field(
            None, description="Execution status (pending, dispatched, completed, failed)"
        )
    ```
  - File: `python-api/app/routers/cpes.py:403-417`:
    ```python
    if "status" in update_data and update_data["status"] is not None:
        new_status = (
            update_data["status"].value
            if isinstance(update_data["status"], Enum)
            else str(update_data["status"])
        )
        if cmd.status in ("completed", "failed") and new_status in ("pending", "dispatched"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot transition command from terminal status '{cmd.status}' to '{new_status}'",
            )
        if cmd.status in ("completed", "failed") and new_status != cmd.status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot transition command from terminal status '{cmd.status}' to '{new_status}'",
            )
        update_data["status"] = new_status
    ```
- **Observed Result**:
  - Invalid status values (e.g. `"bogus_status"`) fail schema validation with `HTTP 422 Unprocessable Entity`.
  - Attempting to rewind a command from `completed` or `failed` back to `pending` or `dispatched` returns `HTTP 400 Bad Request` with `Cannot transition command from terminal status ...`.

### 1.4 Remediation Task 4: Atomicity on Dual Reboot MQTT Publish Failure
- **Prior Defect**: In dual mode, `cpe_pending_commands` record was committed before calling `mqtt_publisher.publish_reboot_command(cpe_id)`. If MQTT failed with 503, an orphan pending reboot command remained in the database.
- **Remediation**:
  - File: `python-api/app/routers/cpes.py:250-258`:
    ```python
    if proto in ("tr369", "dual"):
        try:
            dispatch_info = await mqtt_publisher.publish_reboot_command(cpe_id)
        except Exception as e:
            if pending_cmd is not None:
                await db.delete(pending_cmd)
                await db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"MQTT broker delivery failed: {e}",
            )
    ```
- **Observed Result**: When MQTT fails during dual-stack reboot, `pending_cmd` is cleanly deleted from PostgreSQL before raising `HTTP 503 Service Unavailable`. Direct query in `test_reboot_dual_stack_mqtt_failure_db_side_effect` confirms `len(cmds) == 0` (zero orphan records).

### 1.5 Test Suite Execution Output
- **FastAPI Pytest Suite**:
  ```bash
  $ PYTHONPATH=python-api pytest python-api/tests/ -v
  ============================== 47 passed in 8.14s ==============================
  Exit code: 0
  ```
- **PostgreSQL Schema & Triggers Suite**:
  ```bash
  $ python3 -m unittest discover -s postgres -p "test_*.py" -v
  Ran 74 tests in 1.414s
  OK (skipped=1)
  Exit code: 0
  ```
- **Docker Compose Limits**:
  ```bash
  $ python3 configure_limits.py --test && python3 configure_limits.py --verify
  Ran 10 tests in 0.149s; OK
  All memory limits are valid and correctly configured.
  Exit code: 0
  ```

---

## 2. Logic Chain

1. **Protocol Validation**:
   - Upstream observed that invalid protocol query params silently returned HTTP 200 with false "queued" status while queueing nothing (Observation 1.1).
   - By normalizing `raw_proto.lower().replace("-", "")` and validating against `{"tr069", "tr369", "dual"}` at the start of `reboot_cpe`, any invalid parameter triggers an immediate `HTTPException(400)`.
   - Valid variants (`"TR069"`, `"tr-069"`, `"TR-069"`, `"TR369"`, `"tr-369"`, `"DUAL"`, `"Dual"`) remain fully supported.

2. **Route Parameter UUID Typing**:
   - Upstream observed that `command_id: str` allowed arbitrary strings into SQLAlchemy queries, resulting in `DataError (HTTP 500)` in PostgreSQL (Observation 1.2).
   - Typing `command_id: UUID` in FastAPI route parameters enforces RFC 4122 compliance at HTTP ingestion time. Malformed UUIDs are rejected with HTTP 422 before the database session is touched.
   - Using `str(command_id)` when querying SQLAlchemy matches `UUID_TYPE = Uuid(as_uuid=False)`, providing clean cross-database compatibility across SQLite and PostgreSQL.

3. **Status Enum and Transition Guards**:
   - Upstream observed arbitrary string status inputs and state machine rewind hazards (Observation 1.3).
   - Defining `PendingCommandStatus(str, Enum)` constrains updates strictly to valid lifecycle states (`pending`, `dispatched`, `completed`, `failed`).
   - Checking `cmd.status in ("completed", "failed")` before applying updates prevents terminal commands from being set back to `pending` or `dispatched`, eliminating duplicate execution hazards for the TR-069 worker.

4. **Dual Mode Atomicity**:
   - Upstream observed orphan pending commands left in DB after MQTT broker connection failures (Observation 1.4).
   - Adding compensatory deletion (`await db.delete(pending_cmd); await db.commit()`) in the exception handler guarantees transactional atomicity across the dual-stack dispatch boundary.

5. **Test Alignment & Zero Regressions**:
   - All 47 pytest tests across unit, adversarial, and challenger suites pass with 100% success rate (Observation 1.5).
   - Existing functionality (cascade deletes, OUI validation, live state aliases, reconciliation trigger AST safety, and container memory limits) is fully preserved.

---

## 3. Caveats

- **Scope Boundary**: Axum CWMP listener (port 7547) and XML SOAP parser belong to Milestone 2; they will consume the `cpe_pending_commands` queue finalized here.
- **SQLite In-Memory Test Runner**: Automated unit tests execute against `sqlite+aiosqlite:///:memory:` with PostgreSQL AST static verification (`test_schema.py`, `test_adversarial_m2.py`). Live PostgreSQL container verification is part of Milestone 4.
- No other caveats.

---

## 4. Conclusion

All 4 remediation items identified by `reviewer_m1_1` and `challenger_m1_1` have been implemented:
1. Protocol validation rejects unknown protocols with HTTP 400.
2. Route parameter `command_id: UUID` cleanly returns HTTP 422 on malformed inputs.
3. `PendingCommandStatus` Enum and transition guards prevent terminal state rewinds (HTTP 400).
4. Dual reboot failure compensation eliminates orphan commands (HTTP 503 with 0 orphan rows).

All 47 tests pass. Quality Gate 1 is ready to be declared passed.

---

## 5. Verification Method

To independently reproduce and verify all fixes:

```bash
# 1. Run all pytest unit and adversarial test suites
PYTHONPATH=python-api pytest python-api/tests/ -v

# 2. Run PostgreSQL AST and trigger test suites
python3 -m unittest discover -s postgres -p "test_*.py" -v

# 3. Run Docker Compose limit validator
python3 configure_limits.py --test && python3 configure_limits.py --verify

# 4. Spot check protocol validation (returns 400)
python3 -c '
import asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import Base, get_db
from tests.conftest import test_engine, override_get_db

async def check():
    app.dependency_overrides[get_db] = override_get_db
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/v1/cpes", json={"cpe_id": "c1", "serial_number": "s1", "manufacturer": "m", "model": "m"})
        r = await c.post("/api/v1/cpes/c1/reboot?protocol=invalid")
        assert r.status_code == 400
        print("Protocol validation check: PASS (400)")
asyncio.run(check())
'

# 5. Spot check UUID parameter typing (returns 422)
python3 -c '
import asyncio
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import Base, get_db
from tests.conftest import test_engine, override_get_db

async def check():
    app.dependency_overrides[get_db] = override_get_db
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/cpes/c1/commands/not-a-valid-uuid")
        assert r.status_code == 422
        print("UUID validation check: PASS (422)")
asyncio.run(check())
'
```
