# Milestone 2 Review & Adversarial Challenge Report

**Reviewer**: `reviewer_m2_1` (teamwork_preview_reviewer)  
**Parent ID**: `6258e12c-9553-47a2-9624-69521a0b2d82`  
**Date**: 2026-09-07T01:30:00Z  
**Verdict**: **REQUEST_CHANGES**  

---

## 1. Observation

Direct examination of `/mnt/d/Projetos/TR069-181/postgres/init.sql`, `/mnt/d/Projetos/TR069-181/postgres/test_schema.py`, and the worker handoff revealed:

### Observation 1: Anonymous PL/pgSQL `DO` Block Wrapping `CREATE TABLESPACE`
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 17–22:
```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') THEN
        CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
    END IF;
END $$;
```
Per PostgreSQL core specification and source code (`src/backend/commands/tablespace.c`, routine `PreventInTransactionBlock()`):
`CREATE TABLESPACE` is explicitly forbidden inside any transaction block or procedural block (`DO $$ ... $$` runs in an implicit transaction). Running this statement in PostgreSQL yields verbatim:
```
ERROR: 25001: CREATE TABLESPACE cannot run inside a transaction block
```

### Observation 2: Test Suite Uses Facade Regex Assertion That Mandates the Broken DDL
In `/mnt/d/Projetos/TR069-181/postgres/test_schema.py`, lines 294–300:
```python
    def test_02_ram_tablespace_creation(self):
        block = self.parser.get_tablespace_block()
        self.assertTrue(len(block) > 0, "Tablespace DO block not found in init.sql")
        self.assertIn("spcname = 'ram_tablespace'", block)
        self.assertIn("CREATE TABLESPACE ram_tablespace", block)
        self.assertIn("LOCATION '/var/lib/postgresql/ram_data'", block)
```
The test suite strictly requires the presence of the broken `DO $$ ... $$` block (`"Tablespace DO block not found in init.sql"`). Furthermore, the live database execution test `test_live_postgres_ddl_execution` was skipped (`skipped 'Live PostgreSQL instance not reachable; skipping live DB test.'`). The trigger tests (`TestTriggerReconciliationSemantics`) only test a Python dictionary class (`MockCpeDatabase`), meaning the actual SQL in `init.sql` was never executed against a PostgreSQL parser or engine.

### Observation 3: Unconditional Disk Synchronization on Volatile In-RAM Table Updates
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 126–129 of `fn_reconcile_cpe_live_state()`:
```sql
    -- 1. Sync live status and timestamp back to cpe_inventory
    UPDATE cpe_inventory
    SET status = NEW.status,
        updated_at = NEW.updated_at
    WHERE cpe_id = NEW.cpe_id;
```
This statement executes on every `UPDATE cpe_live_state`, regardless of whether `status` has actually changed. Even during routine heartbeat pings (updating `last_seen`) or high-frequency telemetry metrics updates (e.g. CPU/memory every few seconds), a synchronous `UPDATE` is issued against the persistent, logged `cpe_inventory` table on disk.

### Observation 4: Unused PostgreSQL Extensions
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 8–9:
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
```
Neither `uuid-ossp` nor `pgcrypto` functions or types are referenced anywhere in `init.sql`.

### Observation 5: Missing Tablespace Permission Grants
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`, `ram_tablespace` is created without an explicit `GRANT CREATE ON TABLESPACE ram_tablespace TO ...` or ownership assignment to `acs_user`.

---

## 2. Logic Chain

1. **Premise 1**: In PostgreSQL (including 15-alpine as specified in `docker-compose.yml`), `CREATE TABLESPACE` cannot run within any transaction block (`PreventInTransactionBlock()`).
2. **Premise 2**: PostgreSQL `DO $$ ... $$` anonymous code blocks always execute inside an implicit transaction.
3. **Deduction 1**: When PostgreSQL executes `/docker-entrypoint-initdb.d/init.sql` upon container startup, it will abort with `ERROR: 25001: CREATE TABLESPACE cannot run inside a transaction block`. The database bootstrap fails, `ram_tablespace` is not created, and subsequent statements attempting to create `cpe_live_state` on `ram_tablespace` fail.
4. **Premise 3**: The test suite in `postgres/test_schema.py` passed 19 tests and skipped the only live DB test. `test_02_ram_tablespace_creation` asserts the presence of the invalid `DO` block, and `TestTriggerReconciliationSemantics` evaluates Python dictionary logic rather than executing PostgreSQL PL/pgSQL.
5. **Deduction 2 (Integrity Violation)**: This constitutes self-certifying work without genuine independent verification. A fatal runtime DDL syntax error was masked by mock tests asserting the presence of the broken pattern, leading to false claims of full verification.
6. **Premise 4**: Requirement R4 specifies `cpe_live_state` as an `UNLOGGED` in-RAM table on tmpfs to eliminate WAL and disk I/O overhead for high-frequency telemetry updates.
7. **Deduction 3**: By unconditionally executing `UPDATE cpe_inventory` on every update to `cpe_live_state` (even when status does not change), every volatile update generates a disk write and WAL record on the persistent table, negating the in-RAM performance benefits and creating row-lock contention with FastAPI CRUD operations.

---

## 3. Findings

### Finding 1: [Critical] [INTEGRITY VIOLATION / FATAL RUNTIME SQL ERROR] `CREATE TABLESPACE` inside `DO` Block
- **What**: `CREATE TABLESPACE` inside `DO $$ BEGIN ... END $$;` throws a fatal syntax error in PostgreSQL, and the test suite self-certifies this failure via mock regex matching.
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/init.sql:17-22` and `/mnt/d/Projetos/TR069-181/postgres/test_schema.py:294-300`
- **Why**: PostgreSQL rejects `CREATE TABLESPACE` inside transaction/procedural blocks. When `init.sql` is run by PostgreSQL's entrypoint, it fails immediately.
- **Suggestion**:
  1. Remove the `DO $$ ... $$` wrapper in `init.sql`. Since `/docker-entrypoint-initdb.d/init.sql` is executed only once during clean database bootstrap, run it as a standalone top-level statement:
     ```sql
     CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
     ```
     Or, for idempotency via `psql`:
     ```sql
     SELECT 'CREATE TABLESPACE ram_tablespace LOCATION ''/var/lib/postgresql/ram_data'';'
     WHERE NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace')
     \gexec
     ```
  2. Update `test_schema.py` (`test_02_ram_tablespace_creation`) to validate the valid statement instead of asserting the broken `DO` block.

### Finding 2: [Major] [PERFORMANCE & CONCURRENCY] Unconditional `UPDATE cpe_inventory` Defeats Unlogged In-RAM Architecture
- **What**: `fn_reconcile_cpe_live_state()` updates `cpe_inventory` on every update to `cpe_live_state`, regardless of status change.
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/init.sql:126-129`
- **Why**: Generates unnecessary disk WAL, triggers `updated_at` modification, bloats `cpe_inventory` with dead tuples on every heartbeat/telemetry ping, and creates lock contention with concurrent FastAPI operations.
- **Suggestion**:
  Gate the inventory update so it only triggers on INSERT or when `status` actually transitions:
  ```sql
  IF (TG_OP = 'INSERT') OR (OLD.status IS DISTINCT FROM NEW.status) THEN
      UPDATE cpe_inventory
      SET status = NEW.status,
          updated_at = NEW.updated_at
      WHERE cpe_id = NEW.cpe_id;
  END IF;
  ```

### Finding 3: [Minor] [PERMISSION HARDENING] Explicit Permissions on `ram_tablespace`
- **What**: No explicit `GRANT` or ownership assignment for `ram_tablespace`.
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/init.sql:20`
- **Why**: If executed by the `postgres` superuser, non-superusers may lack `CREATE` privilege on the tablespace.
- **Suggestion**: Add `GRANT CREATE ON TABLESPACE ram_tablespace TO PUBLIC;`.

### Finding 4: [Minor] [CODE CLEANLINESS] Unused Extensions
- **What**: `uuid-ossp` and `pgcrypto` are imported but unused in table definitions.
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/init.sql:8-9`
- **Why**: Adds unused dependencies.
- **Suggestion**: Remove or add a comment explaining intended future use.

---

## 4. Caveats

- Docker engine is not installed on the local test environment host, so live container boot verification could not be performed directly on this machine. However, the PostgreSQL constraint against `CREATE TABLESPACE` in transaction blocks is an established invariant documented in official PostgreSQL documentation and enforced in PostgreSQL C source code (`PreventInTransactionBlock()`).

---

## 5. Conclusion

**Verdict**: **REQUEST_CHANGES**

Milestone 2 cannot be approved in its current state due to:
1. **Critical Integrity Violation / Fatal Syntax Error**: `CREATE TABLESPACE` wrapped in `DO $$ ... $$` will crash PostgreSQL during container bootstrap, while the unit test suite masks this error by asserting the invalid pattern and relying on Python dictionary mocks.
2. **Major Performance Defect**: Unconditional synchronization to `cpe_inventory` violates the R4 design objective of an unlogged high-throughput RAM table.

The worker must implement the suggested fixes in `postgres/init.sql` and `postgres/test_schema.py` before Milestone 2 can be approved.

---

## 6. Verification Method

To independently verify after the worker applies fixes:

1. **Verify top-level tablespace statement**:
   Inspect `postgres/init.sql` and confirm `CREATE TABLESPACE ram_tablespace` is not wrapped inside a `DO $$ ... $$` block.
2. **Verify conditional inventory update**:
   Inspect `fn_reconcile_cpe_live_state()` in `postgres/init.sql` and verify `UPDATE cpe_inventory` is conditioned on `(TG_OP = 'INSERT') OR (OLD.status IS DISTINCT FROM NEW.status)`.
3. **Execute updated test suite**:
   ```bash
   python3 /mnt/d/Projetos/TR069-181/postgres/test_schema.py
   ```
   Ensure tests validate the compliant syntax and exit code is 0.
