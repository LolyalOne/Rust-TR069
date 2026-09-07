# Milestone 4: Python FastAPI Manager — Empirical Adversarial Challenge Report

## Verdict: REQUEST_CHANGES

---

## 1. Observation

Direct empirical observations, reproduction commands, file paths, line numbers, and verbatim outputs:

1. **Existing Test Suite Baseline**:
   - Command: `PYTHONPATH=python-api python3 -m pytest -v python-api/tests/test_api.py`
   - Result: 10 passed in 2.54s. All basic happy-path and baseline error-handling tests passed.

2. **Empirical Adversarial Test Execution**:
   - Test File: `python-api/tests/test_adversarial.py` (9 comprehensive adversarial stress tests covering 404 sweep, duplicate serial handling, 503 broker failures, 50-snapshot cascading deletion, and MQTT reboot regex matching).
   - Command: `PYTHONPATH=python-api python3 -m pytest -v python-api/tests/test_adversarial.py`
   - Result: 8 PASSED, 1 FAILED in 3.28s.
   - Verbatim Failure:
     ```
     FAILED python-api/tests/test_adversarial.py::test_duplicate_serial_on_reregistration_upsert
     ...
     File "python-api/app/routers/cpes.py", line 46, in create_cpe
       await db.commit()
     ...
     sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: cpe_inventory.serial_number
     [SQL: UPDATE cpe_inventory SET serial_number=?, updated_at=CURRENT_TIMESTAMP WHERE cpe_inventory.cpe_id = ?]
     [parameters: ('SN-DEV-1', 'cpe-dev-2')]
     ```

3. **Code Inspection of Vulnerable Endpoint (`python-api/app/routers/cpes.py`)**:
   - Lines 35–50 (`create_cpe` re-registration / upsert branch):
     ```python
     existing = await db.get(CpeInventory, payload.cpe_id)
     if existing:
         # Update existing record with authoritative registration data
         existing.serial_number = payload.serial_number
         existing.manufacturer = payload.manufacturer
         existing.model = payload.model
         existing.oui = payload.oui
         existing.product_class = payload.product_class
         existing.hardware_version = payload.hardware_version
         existing.software_version = payload.software_version
         existing.description = payload.description
         await db.commit()
         await db.refresh(existing)
         response.status_code = status.HTTP_200_OK
         return existing
     ```
   - Notice that `await db.commit()` at line 46 is **NOT** enclosed within a `try ... except IntegrityError` block.
   - In contrast, lines 51–63 (`create_cpe` new CPE insertion branch) specifically catch `IntegrityError`:
     ```python
     new_cpe = CpeInventory(**payload.model_dump())
     db.add(new_cpe)
     try:
         await db.commit()
         await db.refresh(new_cpe)
         return new_cpe
     except IntegrityError as err:
         await db.rollback()
         raise HTTPException(
             status_code=status.HTTP_409_CONFLICT,
             detail=f"CPE with serial_number '{payload.serial_number}' already exists: {err}",
         )
     ```

4. **Schema Omission (`python-api/app/schemas.py`)**:
   - In `CpeBase` (line 16): `oui: Optional[str] = Field(None, max_length=6, description="Organizationally Unique Identifier")`.
   - In `CpeUpdate` (line 30): `oui: Optional[str] = None` without `max_length=6`.
   - Database column `cpe_inventory.oui` is `VARCHAR(6)` (`init.sql:31`).

5. **Successful Empirical Verifications**:
   - **404 Not Found Sweep**: Tested against `GET /cpes/{id}`, `PUT /cpes/{id}`, `PATCH /cpes/{id}`, `DELETE /cpes/{id}`, `GET /cpes/{id}/live-state`, `GET /cpes/{id}/live`, `GET /cpes/{id}/history`, and `POST /cpes/{id}/reboot`. All cleanly returned HTTP 404 with descriptive detail.
   - **404 Unreported Live State**: Tested `GET /cpes/{id}/live-state` when device exists in inventory but has no telemetry. Cleanly returned HTTP 404 `"Live state not available yet for CPE '{cpe_id}'"`.
   - **503 Broker Failure**: Mocked broker `ConnectionRefusedError` and `TimeoutError` during `POST /cpes/{id}/reboot`. Returned HTTP 503 `"MQTT broker delivery failed: ..."`.
   - **Cascading Deletion**: Registered a CPE, injected 1 live-state record and 50 historical metric snapshots. Invoked `DELETE /api/v1/cpes/{cpe_id}`. Returned HTTP 200, and direct DB verification confirmed 0 remaining rows in `cpe_live_state` and 0 remaining rows in `cpe_historical_metrics`.
   - **MQTT Reboot Payload Format**: Verified publication to `usp/endpoint/{cpe_id}/request` with QoS 1. Verified payload JSON matches `simulate_flow.sh` regex `reboot|operate` (both in Python regex and bash `grep -qi "reboot\|operate"`).

---

## 2. Logic Chain

1. From Observation 3, `POST /api/v1/cpes` is designed to support both device creation and upsert/re-registration (to reconcile devices auto-discovered by the Rust Core worker).
2. When device `cpe-1` has `serial_number: "SN-1"` and device `cpe-2` has `serial_number: "SN-2"`, a subsequent registration request for `cpe-2` with `serial_number: "SN-1"` executes the `if existing:` branch.
3. At line 46 (`cpes.py`), `await db.commit()` executes an `UPDATE cpe_inventory SET serial_number = 'SN-1' WHERE cpe_id = 'cpe-2'`.
4. Because `serial_number` is unique (`cpe_inventory.serial_number UNIQUE NOT NULL`), the database engine raises an `IntegrityError`.
5. Because there is no `try ... except IntegrityError` block around `await db.commit()` at line 46, the exception escapes the route handler, unhandled.
6. FastAPI/Starlette's top-level exception handler catches this unhandled exception and returns HTTP 500 Internal Server Error instead of the mandated HTTP 409 Conflict.
7. Furthermore, the database transaction is aborted without an explicit rollback, leaving the session dirty until disposed.
8. Therefore, the implementation violates Requirement 1 ("Test error handling: 404 on non-existent CPE, 409 on duplicate serial_number, 503 on MQTT broker failure") in the re-registration/upsert path.

---

## 3. Caveats

- **PostgreSQL Execution**: Unit and adversarial tests ran using `aiosqlite` with foreign keys enabled (`PRAGMA foreign_keys=ON`), which mirrors PostgreSQL foreign key cascading behavior. Production deployment runs on PostgreSQL 15 with asyncpg. The unique constraint on `serial_number` exists identically in `init.sql:28` (`serial_number VARCHAR(64) UNIQUE NOT NULL`).
- **Simulate Flow Impact**: `simulate_flow.sh` generates a fresh serial number with `$(date +%s)` and explicitly executes `DELETE /api/v1/cpes/${CPE_ID}` before registration, so `simulate_flow.sh` exercises only the insert path (which currently catches the error). However, in realistic ACS deployments with auto-discovery and concurrent provisioning, re-registration conflicts will occur and must return HTTP 409 rather than HTTP 500.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

The Python FastAPI Manager (Milestone 4) is exceptionally well-structured and passes 4 out of 5 required criteria with flying colors (404 handling, 503 handling, cascading deletion with heavy history, and MQTT reboot payload regex compliance).

However, an unhandled `IntegrityError` bug was empirically identified in `python-api/app/routers/cpes.py`:
- **Bug**: `POST /api/v1/cpes` returns HTTP 500 instead of HTTP 409 when re-registering an existing `cpe_id` with a duplicate `serial_number`.
- **Required Fix**: Wrap `await db.commit()` and `await db.refresh(existing)` in lines 46–48 of `python-api/app/routers/cpes.py` with `try ... except IntegrityError as err:` that executes `await db.rollback()` and raises `HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"CPE with serial_number '{payload.serial_number}' already exists: {err}")`.
- **Recommended Hardening**: In `python-api/app/schemas.py`, add `max_length=6` to `CpeUpdate.oui`.

---

## 5. Verification Method

To independently verify this finding and confirm the fix once implemented:

1. **Reproduce the Bug**:
   ```bash
   PYTHONPATH=python-api python3 -m pytest -v -k "test_duplicate_serial_on_reregistration_upsert" python-api/tests/test_adversarial.py
   ```
   *Current Observation*: Fails with uncaught `sqlalchemy.exc.IntegrityError` and HTTP 500.

2. **Verify After Fix**:
   Apply the `try ... except IntegrityError` fix in `python-api/app/routers/cpes.py`, then run:
   ```bash
   PYTHONPATH=python-api python3 -m pytest -v python-api/tests/
   ```
   *Expected Result*: All 19 tests across `test_api.py` (10 tests) and `test_adversarial.py` (9 tests) pass with exit code 0.
