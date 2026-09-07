# Milestone 1 Code Review and Adversarial Challenge Report

**Reviewer**: `reviewer_m1_1` (Roles: Reviewer, Adversarial Critic)  
**Milestone**: Milestone 1 — Infra & Data Layer (Dual-Stack TR-069 / TR-369)  
**Target Repository**: `Rust-TR069`  
**Date**: 2026-09-07T15:00:00Z  
**Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### 1.1 Integrity Audit (Zero Integrity Violations)
We audited the source tree and diffs across `docker-compose.yml`, `postgres/init.sql`, `python-api/app/models.py`, `python-api/app/schemas.py`, `python-api/app/routers/cpes.py`, and `python-api/tests/test_api.py`.
- **No hardcoded test outputs or dummy return values**: All routes query the database and publish to the MQTT client.
- **No facade implementations**: The `cpe_pending_commands` table, foreign keys, cascade triggers, ORM models, Pydantic schemas, and FastAPI endpoints implement real business and data persistence logic.
- **No shortcuts**: Docker Compose limits, environment variables, PostgreSQL tablespace safety, and backward compatibility with TR-369 USP were fully preserved.
- **No fabricated logs**: All test runs independently executed and produced identical results.

### 1.2 Verification Command Executions

1. **Docker Compose Limits & Syntax Verification**:
   ```bash
   $ python3 configure_limits.py --test && python3 configure_limits.py --show && python3 configure_limits.py --verify
   Ran 10 tests in 0.131s
   OK
   All memory limits are valid and correctly configured. (postgres: 1.5G, mosquitto: 500M, rust-core: 500M, python-api: 1G)
   Exit code: 0
   ```

2. **PostgreSQL Schema, AST & Trigger Verification**:
   ```bash
   $ python3 -m unittest discover -s postgres -p "test_*.py" -v
   Ran 68 tests in 1.185s
   OK (skipped=1)
   Exit code: 0
   ```

3. **FastAPI Unit & Adversarial Test Suites**:
   ```bash
   $ PYTHONPATH=python-api pytest python-api/tests/ -v
   ============================== 30 passed in 12.71s ==============================
   Exit code: 0
   ```

### 1.3 Code Inspection & Verbatim Observations

#### Observation A: Silent Success on Invalid Protocol in `reboot_cpe`
Location: `python-api/app/routers/cpes.py:229-272`
```python
229:     proto = (protocol or "dual").lower()
230: 
231:     # If TR-069 or dual, enqueue in cpe_pending_commands
232:     pending_cmd = None
233:     if proto in ("tr069", "tr-069", "dual"):
234:         pending_cmd = CpePendingCommand(...)
...
245:     if proto in ("tr369", "tr-369", "dual"):
246:         ...
247:         return CommandDispatchResponse(...)
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
**Empirical Result**: Calling `POST /api/v1/cpes/{cpe_id}/reboot?protocol=invalid_proto` returned HTTP 200 OK with `{"status": "queued", "command_key": "reboot-test-proto-bug", "topic": "tr069/cwmp"}`. A subsequent `GET /api/v1/cpes/{cpe_id}/commands` confirmed that `[]` was returned—nothing was queued in PostgreSQL, and no message was dispatched to MQTT.

#### Observation B: PostgreSQL Type Casting Failure on Malformed `command_id`
Location: `python-api/app/routers/cpes.py:346` (`get_cpe_command`) and `line 372` (`update_cpe_command`)
```python
344: async def get_cpe_command(
345:     cpe_id: str,
346:     command_id: str,
347:     db: AsyncSession = Depends(get_db),
348: ):
...
354:     stmt = select(CpePendingCommand).where(
355:         CpePendingCommand.cpe_id == cpe_id,
356:         CpePendingCommand.id == command_id,
357:     )
```
In `python-api/app/models.py:28, 163-167`:
```python
UUID_TYPE = Uuid(as_uuid=False).with_variant(String(36), "sqlite")
...
id: Mapped[str] = mapped_column(UUID_TYPE, primary_key=True, default=lambda: str(uuid.uuid4()))
```
**Empirical SQL Compilation with PostgreSQL dialect**:
```python
stmt = select(CpePendingCommand).where(CpePendingCommand.id == "not-a-valid-uuid")
stmt.compile(dialect=postgresql.dialect())
# Yields: WHERE cpe_pending_commands.id = %(id_1)s::UUID with {'id_1': 'not-a-valid-uuid'}
```
In PostgreSQL, executing `::UUID` on a non-UUID string raises `ERROR: invalid input syntax for type uuid: "not-a-valid-uuid"`, which triggers an unhandled DB exception resulting in HTTP 500 Internal Server Error in production. In SQLite unit tests, this was masked because SQLite treats the column as `VARCHAR(36)` and simply returned 404.

#### Observation C: Partial Write / Lack of Atomicity on MQTT Failure in Dual-Stack Reboot
Location: `python-api/app/routers/cpes.py:233-253`
```python
240:         db.add(pending_cmd)
241:         await db.commit()
242:         await db.refresh(pending_cmd)
243: 
244:     # If TR-369 or dual, publish to MQTT
245:     if proto in ("tr369", "tr-369", "dual"):
246:         try:
247:             dispatch_info = await mqtt_publisher.publish_reboot_command(cpe_id)
248:         except Exception as e:
249:             raise HTTPException(
250:                 status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
251:                 detail=f"MQTT broker delivery failed: {e}",
252:             )
```
**Empirical Result**: When MQTT publisher was mocked to fail (`ConnectionRefusedError`), `POST /reboot` returned HTTP 503 as expected. However, inspection of `cpe_pending_commands` revealed that an orphan pending reboot command (`fe1d80b5-...`) was committed in PostgreSQL. Each retry created another orphan command.

#### Observation D: Status Field Allows Arbitrary Strings
Location: `python-api/app/schemas.py:113-117`
```python
class PendingCommandUpdate(BaseModel):
    status: Optional[str] = Field(None, max_length=32, description="Execution status (e.g. dispatched, completed, failed)")
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_payload: Optional[dict[str, Any]] = None
```
`status` accepts any arbitrary string up to 32 characters (or empty string `""`), instead of validating against the domain model statuses: `'pending'`, `'dispatched'`, `'completed'`, `'failed'`.

---

## 2. Logic Chain

1. **Infrastructure & Compose (`docker-compose.yml`)**:
   - Port `7547:7547` and environment variables `CWMP_PORT=7547`, `CWMP_HOST=0.0.0.0` were added to `rust-core` cleanly.
   - Resource limits (500M) and YAML structure remain 100% compliant with `configure_limits.py`.

2. **Schema & Triggers (`postgres/init.sql`)**:
   - `cpe_pending_commands` table and `idx_cpe_pending_commands_lookup` index were added at the bottom of the script.
   - Foreign key has `ON DELETE CASCADE`.
   - `CREATE TABLESPACE` remains outside transaction blocks.
   - Existing triggers on `cpe_live_state` and optical power reconciliation remain undisturbed with zero WAL amplification.

3. **API Logic Chain from Observations to Conclusion**:
   - *Observation A* demonstrates that invalid protocol arguments bypass both queueing and MQTT publishing while returning a false 200 OK. This is an input validation defect.
   - *Observation B* demonstrates that because `command_id` is an untyped string in FastAPI route parameters, PostgreSQL's dialect performs an explicit `::UUID` cast. A malformed UUID in production generates a database DataError resulting in HTTP 500 rather than HTTP 422 or 404. This defect was concealed by the SQLite test runner.
   - *Observation C* demonstrates that in dual-stack mode, `db.commit()` precedes MQTT dispatch without compensation or rollback on failure. When MQTT is unavailable, the caller receives HTTP 503, but the TR-069 queue receives an uncoordinated orphan command.
   - *Observation D* shows missing enum/literal validation on `PendingCommandUpdate.status`, allowing corruption of the status machine.

---

## 3. Caveats

- **Scope Boundary**: Axum HTTP listener, XML SOAP parsing, and CWMP session engine belong to Milestone 2 and 3; their absence here is planned and expected.
- **Docker Daemon Live Test**: Live multi-container tests are deferred to Milestone 4 per `PROJECT.md`. All local unit and AST tests were verified.
- **No Other Caveats**.

---

## 4. Quality Review Report

### Review Summary
**Verdict**: **REQUEST_CHANGES**

### Findings

#### [Major] Finding 1: Silent Success with False 200 OK on Invalid `protocol` Parameter
- **What**: `POST /api/v1/cpes/{cpe_id}/reboot?protocol=<invalid>` returns HTTP 200 OK claiming `status: queued`, but does not queue anything in DB and does not publish to MQTT.
- **Where**: `python-api/app/routers/cpes.py:229-272`
- **Why**: Violates API predictability and silently drops reboot requests.
- **Suggestion**:
  ```python
  allowed_protocols = {"tr069", "tr-069", "tr369", "tr-369", "dual"}
  proto = (protocol or "dual").lower()
  if proto not in allowed_protocols:
      raise HTTPException(
          status_code=status.HTTP_400_BAD_REQUEST,
          detail=f"Invalid protocol '{protocol}'. Allowed: tr069, tr369, dual",
      )
  ```

#### [Major] Finding 2: Unvalidated `command_id: str` Causes PostgreSQL `DataError` (HTTP 500)
- **What**: Passing a non-UUID string to `GET /api/v1/cpes/{cpe_id}/commands/{command_id}` or `PATCH` compiles to `cpe_pending_commands.id = %(id)s::UUID` in PostgreSQL, raising an unhandled DB exception.
- **Where**: `python-api/app/routers/cpes.py:346, 372`
- **Why**: Malformed user input must return HTTP 422 or 404, never HTTP 500.
- **Suggestion**: Type `command_id: UUID` in the FastAPI endpoint signatures or validate with a UUID regex:
  ```python
  from uuid import UUID
  ...
  async def get_cpe_command(
      cpe_id: str,
      command_id: UUID,
      db: AsyncSession = Depends(get_db),
  ):
      ...
      stmt = select(CpePendingCommand).where(
          CpePendingCommand.cpe_id == cpe_id,
          CpePendingCommand.id == str(command_id),
      )
  ```
  FastAPI will automatically return HTTP 422 for malformed UUID inputs before executing the SQL query.

#### [Medium] Finding 3: Lack of Atomicity on Dual-Stack Reboot when MQTT Fails
- **What**: Committing the DB transaction before MQTT dispatch leaves orphan pending commands in the queue when MQTT fails with 503.
- **Where**: `python-api/app/routers/cpes.py:233-253`
- **Why**: Callers receiving 503 retry and multiply pending reboot commands in the database.
- **Suggestion**: Perform MQTT publish first, or defer `await db.commit()` until after MQTT publish succeeds:
  ```python
  if proto in ("tr369", "tr-369", "dual"):
      try:
          dispatch_info = await mqtt_publisher.publish_reboot_command(cpe_id)
      except Exception as e:
          await db.rollback()
          raise HTTPException(
              status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
              detail=f"MQTT broker delivery failed: {e}",
          )
  if pending_cmd:
      await db.commit()
      await db.refresh(pending_cmd)
  ```

#### [Minor] Finding 4: Unconstrained `status` in `PendingCommandUpdate`
- **What**: `PendingCommandUpdate.status` allows arbitrary strings up to 32 characters.
- **Where**: `python-api/app/schemas.py:113-117`
- **Why**: Invalid statuses can break downstream state transitions in `rust-core`.
- **Suggestion**: Use `Literal["pending", "dispatched", "completed", "failed"]` or an enum.

### Verified Claims
- `configure_limits.py --verify` reports all 4 services VALID -> PASS
- `python3 -m unittest discover -s postgres -p "test_*.py" -v` -> 68 tests ran, 67 passed, 1 skipped -> PASS
- `PYTHONPATH=python-api pytest python-api/tests/ -v` -> 30 passed -> PASS
- Zero WAL write amplification preserved -> PASS
- Cascade delete removes pending commands -> PASS

---

## 5. Adversarial Challenge Report

### Challenge Summary
**Overall risk assessment**: **MEDIUM** (High risk of production 500 errors on PostgreSQL and silent command drops if not addressed before M2).

### Challenges & Stress Test Results
1. **Challenge 1 (Silent Protocol Drop)**:
   - *Attack*: Send `POST /api/v1/cpes/123/reboot?protocol=bogus`.
   - *Result*: Received 200 OK, zero actions taken. **FAIL** (Vulnerability confirmed).
2. **Challenge 2 (PostgreSQL DataError 500 on Non-UUID)**:
   - *Attack*: Target PostgreSQL dialect with `WHERE id = 'invalid-uuid'`.
   - *Result*: Generated `::UUID` cast which raises `DataError` on PostgreSQL. **FAIL** (Vulnerability confirmed).
3. **Challenge 3 (Partial Write on Broker Failure)**:
   - *Attack*: Trigger reboot with broker down in dual mode.
   - *Result*: Returns 503 but leaves committed pending command in DB. **FAIL** (Atomicity broken).

---

## 6. Conclusion

Milestone 1 shows solid architectural work, clean DDL, correct Docker Compose limits, and zero integrity violations. However, because of the two **Major** defects (silent failure on invalid protocol and PostgreSQL 500 crash on malformed UUID) and one **Medium** defect (atomicity break on broker failure), changes are requested to ensure robust error handling, SQL safety, and Pydantic validation before advancing to Milestone 2.

**Verdict**: **REQUEST_CHANGES**

---

## 7. Verification Method

1. **Verify Existing Suites**:
   ```bash
   python3 configure_limits.py --verify
   python3 -m unittest discover -s postgres -p "test_*.py" -v
   PYTHONPATH=python-api pytest python-api/tests/ -v
   ```
2. **Verify Protocol Validation Fix**:
   ```bash
   DATABASE_URL=sqlite+aiosqlite:///:memory: python3 -c '
   import asyncio
   from httpx import AsyncClient, ASGITransport
   from app.main import app
   from app.database import engine, Base

   async def test():
       async with engine.begin() as conn:
           await conn.run_sync(Base.metadata.create_all)
       async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
           await client.post("/api/v1/cpes", json={"cpe_id": "c1", "serial_number": "s1", "manufacturer": "m", "model": "m"})
           r = await client.post("/api/v1/cpes/c1/reboot?protocol=invalid")
           assert r.status_code == 400
   asyncio.run(test())
   '
   ```
3. **Verify UUID Validation Fix**:
   ```bash
   DATABASE_URL=sqlite+aiosqlite:///:memory: python3 -c '
   import asyncio
   from httpx import AsyncClient, ASGITransport
   from app.main import app
   from app.database import engine, Base

   async def test():
       async with engine.begin() as conn:
           await conn.run_sync(Base.metadata.create_all)
       async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
           await client.post("/api/v1/cpes", json={"cpe_id": "c1", "serial_number": "s1", "manufacturer": "m", "model": "m"})
           r = await client.get("/api/v1/cpes/c1/commands/invalid-uuid")
           assert r.status_code == 422
   asyncio.run(test())
   '
   ```
