# Milestone 2 Review & Adversarial Challenge Report: PostgreSQL Hybrid Schema & Triggers

**Reviewer**: `reviewer_m2_2` (Teamwork Preview Reviewer & Critic)  
**Parent ID**: `6258e12c-9553-47a2-9624-69521a0b2d82`  
**Date**: 2026-09-07T01:30:00Z  
**Target Files**:
- `/mnt/d/Projetos/TR069-181/postgres/init.sql`
- `/mnt/d/Projetos/TR069-181/postgres/test_schema.py`

---

## Review Summary

**Verdict**: **REQUEST_CHANGES**

### Executive Summary
While the schema architecture largely adheres to the specifications in `ORIGINAL_REQUEST.md` (R4) and `PROJECT.md` (Feature 6–10), the implementation contains **one critical runtime-breaking DDL defect**, **one major architectural performance vulnerability**, and **one test suite self-certification flaw**:

1. **Critical Defect (DDL Syntax / Execution)**: `init.sql` wraps `CREATE TABLESPACE` inside a PL/pgSQL `DO $$ ... $$` anonymous code block. In PostgreSQL, `CREATE TABLESPACE` cannot be executed inside a transaction block. Since `DO` blocks execute within an implicit transaction, executing `init.sql` on a real PostgreSQL instance aborts with `ERROR: CREATE TABLESPACE cannot be executed inside a transaction block`. This breaks container initialization during `docker compose up`.
2. **Major Defect (Heartbeat WAL Amplification)**: The trigger function `fn_reconcile_cpe_live_state()` unconditionally executes `UPDATE cpe_inventory ...` on *every* update of `cpe_live_state`, even when only `last_seen` changes. This forces WAL writes and disk I/O on the persistent logged table for every volatile ping, negating the purpose of an `UNLOGGED` RAM tmpfs table.
3. **Major Defect (Test Self-Certification & Masked Error)**: `test_schema.py` does not execute DDL against a SQL engine. Instead, it tests an in-memory Python dictionary mock (`MockCpeDatabase`) and uses regular expressions to assert the presence of the invalid `DO $$` block, actively cementing the broken syntax instead of catching it.

---

## 1. Observation

Direct examination of code, specifications, and test executions revealed:

### Observation 1.1: `CREATE TABLESPACE` inside `DO $$` Block
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 17–22:
```sql
17: DO $$
18: BEGIN
19:     IF NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') THEN
20:         CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
21:     END IF;
22: END $$;
```
- In PostgreSQL, `DO $$ ... $$` blocks are executed within an implicit transaction block.
- Per PostgreSQL official documentation and engine rules (`src/backend/commands/tablespace.c`), `CREATE TABLESPACE` cannot be executed inside a transaction block (`ERROR: CREATE TABLESPACE cannot be executed inside a transaction block`).

### Observation 1.2: Unconditional `UPDATE cpe_inventory` on Routine Heartbeats
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 125–130:
```sql
125:     -- 1. Sync live status and timestamp back to cpe_inventory
126:     UPDATE cpe_inventory
127:     SET status = NEW.status,
128:         updated_at = NEW.updated_at
129:     WHERE cpe_id = NEW.cpe_id;
```
- Attached trigger: `CREATE TRIGGER trg_cpe_live_state_reconcile AFTER INSERT OR UPDATE ON cpe_live_state FOR EACH ROW EXECUTE FUNCTION fn_reconcile_cpe_live_state();`
- When a heartbeat arrives updating only `last_seen` in `cpe_live_state`, lines 135–149 correctly refrain from inserting into `cpe_state_history` (`v_should_record = FALSE`).
- However, lines 126–129 execute an `UPDATE` on `cpe_inventory` unconditionally on every single heartbeat, generating continuous WAL generation and disk I/O on the permanent table.

### Observation 1.3: Test Suite Execution & Mock Self-Certification
Execution of the mandated test suite:
```bash
python3 -m unittest discover -s /mnt/d/Projetos/TR069-181/postgres -p "test_*.py" -v
```
Output:
```
test_live_postgres_ddl_execution (test_schema.TestLivePostgresIntegration.test_live_postgres_ddl_execution) ... skipped 'Live PostgreSQL instance not reachable; skipping live DB test.'
test_01_init_sql_file_exists_and_not_empty (test_schema.TestPostgresSchemaDDL.test_01_init_sql_file_exists_and_not_empty) ... ok
test_02_ram_tablespace_creation (test_schema.TestPostgresSchemaDDL.test_02_ram_tablespace_creation) ... ok
...
Ran 20 tests in 0.020s
OK (skipped=1)
```
Inspection of `/mnt/d/Projetos/TR069-181/postgres/test_schema.py`:
- `test_02_ram_tablespace_creation` (lines 294–300):
```python
294:     def test_02_ram_tablespace_creation(self):
295:         block = self.parser.get_tablespace_block()
296:         self.assertTrue(len(block) > 0, "Tablespace DO block not found in init.sql")
297:         self.assertIn("spcname = 'ram_tablespace'", block)
298:         self.assertIn("CREATE TABLESPACE ram_tablespace", block)
299:         self.assertIn("LOCATION '/var/lib/postgresql/ram_data'", block)
```
- The test verifies that `CREATE TABLESPACE` is embedded inside a `DO` block.
- In `TestTriggerReconciliationSemantics`, lines 406–593 test `MockCpeDatabase` (a Python dict class), not the PL/pgSQL trigger in `init.sql`.

### Observation 1.4: Schema Table and Index Structure
In `/mnt/d/Projetos/TR069-181/postgres/init.sql`:
- `cpe_inventory`: Persistent table with primary key `cpe_id VARCHAR(128)`, unique `serial_number`, and B-tree indexes on `(manufacturer, model)` and `status`.
- `cpe_live_state`: `UNLOGGED TABLE ... TABLESPACE ram_tablespace`.
  - Column `cpe_id VARCHAR(128) PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`.
  - GIN indexes on `current_parameters` and `telemetry_metrics`.
  - B-tree indexes on `status` and `last_seen`.
- `cpe_state_history`: Persistent table with `id BIGSERIAL PRIMARY KEY`, `cpe_id REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`.
  - Compound B-tree index on `(cpe_id, recorded_at DESC)`.
  - GIN index on `telemetry_metrics`.
  - B-tree index on `recorded_at`.

---

## 2. Logic Chain

1. **Tablespace Bootstrap Failure**:
   - `init.sql` is mounted to `/docker-entrypoint-initdb.d/init.sql` in `docker-compose.yml`.
   - When PostgreSQL initializes the cluster, it runs all `.sql` scripts in `/docker-entrypoint-initdb.d/` via `psql`.
   - PostgreSQL prohibits `CREATE TABLESPACE` from running inside any transaction or `DO` block because tablespace filesystem symlinks cannot be rolled back.
   - When `psql` reaches lines 17–22 of `init.sql`, the PostgreSQL server throws `ERROR: CREATE TABLESPACE cannot be executed inside a transaction block` and aborts the initialization script.
   - Therefore, container startup fails.

2. **Heartbeat Noise & WAL Invalidation**:
   - `ORIGINAL_REQUEST.md` (R4) and `PROJECT.md` mandate an `UNLOGGED` RAM-based table (`cpe_live_state`) specifically to avoid Write-Ahead Logging (WAL) and disk bottleneck for high-frequency telemetry/heartbeats.
   - While `fn_reconcile_cpe_live_state()` filters heartbeat noise from `cpe_state_history` (leaving history lean), step 1 updates `cpe_inventory` on every call.
   - Because `cpe_inventory` is a standard logged table, updating it on every heartbeat writes to the WAL and fsyncs to disk, destroying the intended zero-WAL performance benefit of the RAM table.

3. **Test Gap & Self-Certification**:
   - The offline test suite `test_schema.py` provides no actual SQL engine validation.
   - In `test_02_ram_tablespace_creation`, the test strictly expects the broken `DO $$ ... CREATE TABLESPACE ... $$;` construct.
   - Any worker running the tests sees `OK (skipped=1)` and assumes the DDL is valid, creating a false guarantee of correctness.

---

## 3. Findings

### Finding 1 [CRITICAL]: `CREATE TABLESPACE` in `DO $$` Block Causes Container Initialization Abort
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 17–22
- **Why**: PostgreSQL rejects `CREATE TABLESPACE` within transaction blocks (including `DO` blocks). Container initialization fails immediately.
- **Suggestion**: Replace the `DO $$ ... $$` block with a top-level, standalone SQL statement as specified in `PROJECT.md` Feature 6:
  ```sql
  CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
  ```
  In `/docker-entrypoint-initdb.d/`, initialization scripts run exactly once against a new cluster, making a top-level statement safe. Alternatively, if re-run idempotence is needed in `psql`, use:
  ```sql
  SELECT 'CREATE TABLESPACE ram_tablespace LOCATION ''/var/lib/postgresql/ram_data'''
  WHERE NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') \gexec
  ```
  Also update `test_schema.py` line 294–300 to match.

### Finding 2 [MAJOR]: Unconditional `UPDATE cpe_inventory` Causes Continuous WAL Logging on Heartbeats
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/init.sql`, lines 126–129
- **Why**: Every heartbeat (updating only `last_seen` in `cpe_live_state`) triggers an update on `cpe_inventory`, incurring WAL and disk I/O on a persistent table.
- **Suggestion**: Gate the synchronization so `cpe_inventory` is only updated when `status` actually transitions (or on `INSERT`):
  ```sql
  IF (TG_OP = 'INSERT') OR (OLD.status IS DISTINCT FROM NEW.status) THEN
      UPDATE cpe_inventory
      SET status = NEW.status,
          updated_at = NEW.updated_at
      WHERE cpe_id = NEW.cpe_id;
  END IF;
  ```

### Finding 3 [MAJOR]: Test Harness Asserts Broken Syntax and Masks DDL Flaws
- **Where**: `/mnt/d/Projetos/TR069-181/postgres/test_schema.py`, lines 294–300 (`test_02_ram_tablespace_creation`)
- **Why**: The test asserts that `CREATE TABLESPACE` is inside a `DO` block, preventing the author from recognizing the invalid PostgreSQL syntax.
- **Suggestion**: Update `test_schema.py` to parse and assert top-level `CREATE TABLESPACE` DDL.

---

## 4. Adversarial Challenge & Stress Test Results

### Challenge 1: PostgreSQL DDL Syntax & Tablespace Execution
- **Assumption Challenged**: PL/pgSQL `DO $$ ... CREATE TABLESPACE ... $$;` is valid PostgreSQL syntax.
- **Attack Scenario**: Execute `init.sql` against a standard PostgreSQL 15/16 engine.
- **Blast Radius**: `CRITICAL` — Container entrypoint fails, preventing database initialization.
- **Result**: **FAIL** (confirmed via PostgreSQL documentation and source code constraints).

### Challenge 2: Heartbeat Telemetry Flood Resistance
- **Assumption Challenged**: Heartbeat updates (`last_seen`) do not produce disk/WAL overhead.
- **Attack Scenario**: 5,000 CPEs send periodic heartbeat pings every 5 seconds, updating `cpe_live_state.last_seen`.
- **Blast Radius**: `HIGH` — `cpe_inventory` suffers 1,000 writes/sec to WAL and disk despite `cpe_live_state` being an in-memory unlogged table.
- **Result**: **FAIL** on `cpe_inventory` write amplification; **PASS** on `cpe_state_history` row filtering.

### Challenge 3: Simultaneous Status and Metric Changes
- **Scenario**: CPE transitions `status` to `'error'` and simultaneously reports high `cpu_usage: 99.9`.
- **Expected Behavior**: Single snapshot in `cpe_state_history` with `change_reason = 'status_and_metrics_changed'`.
- **Result**: **PASS**. Handled by first `ELSIF` branch.

### Challenge 4: Cross-Persistence Foreign Key Validity
- **Assumption Challenged**: Can `UNLOGGED` table `cpe_live_state` have a Foreign Key referencing `LOGGED` table `cpe_inventory`?
- **Analysis**: PostgreSQL C source code (`src/backend/commands/tablecmds.c`) enforces:
  - Permanent tables may only reference permanent tables.
  - Unlogged tables may reference permanent or unlogged tables.
- **Result**: **PASS**. Both FKs reference the permanent `cpe_inventory` table.

### Challenge 5: Foreign Key Cascade Deletion
- **Scenario**: Deleting a record from `cpe_inventory`.
- **Expected Behavior**: Corresponding entries in `cpe_live_state` and `cpe_state_history` are deleted automatically.
- **Result**: **PASS**. Both child tables declare `ON DELETE CASCADE`.

---

## 5. Verified Claims vs Unverified Items

### Verified Claims
- `cpe_inventory` table definition, data types, constraints, and B-tree indexes match specifications → **VERIFIED**
- `cpe_live_state` UNLOGGED status, RAM tablespace assignment, GIN indexes on `current_parameters` and `telemetry_metrics`, B-tree on `status` and `last_seen` → **VERIFIED**
- `cpe_state_history` BIGSERIAL primary key, compound index `(cpe_id, recorded_at DESC)`, GIN index on `telemetry_metrics` → **VERIFIED**
- Trigger noise filtering prevents `cpe_state_history` from recording routine heartbeat pings → **VERIFIED**
- Simultaneous status and metric updates properly tagged and recorded → **VERIFIED**
- FK cascade delete semantics accurately configured → **VERIFIED**
- Unit test suite `python3 -m unittest discover -s postgres -p "test_*.py" -v` executes with exit code 0 → **VERIFIED**

### Unverified Items / Coverage Gaps
- Live execution in running PostgreSQL container: skipped because no PostgreSQL daemon was active on the host during this review pass. Verified analytically against PostgreSQL 16 engine specifications.

---

## 6. Caveats

- A running PostgreSQL daemon was not active on the review host (only `psql` client was present); runtime behavior was verified by deep static inspection, official PostgreSQL 16 documentation, and engine source code analysis.

---

## 7. Conclusion & Actionable Next Steps

**Verdict**: **REQUEST_CHANGES**

To unblock Milestone 2 and allow approval:
1. **Fix `init.sql` Tablespace Syntax**: Remove the `DO $$ ... $$` wrapper around `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';`.
2. **Prevent Heartbeat WAL Amplification**: In `fn_reconcile_cpe_live_state()`, wrap the `UPDATE cpe_inventory` statement in `IF (TG_OP = 'INSERT') OR (OLD.status IS DISTINCT FROM NEW.status) THEN ... END IF;`.
3. **Update `test_schema.py`**: Update `test_02_ram_tablespace_creation` so that it validates the standalone `CREATE TABLESPACE` DDL statement without requiring the `DO $$` block.
4. **Re-run Test Suite**: Verify all 20 tests pass cleanly with the updated DDL.

---

## 8. Verification Method

To independently verify the findings in this report:

1. **Verify `CREATE TABLESPACE` restriction in PostgreSQL docs**:
   Inspect PostgreSQL documentation on `CREATE TABLESPACE` or test in `psql`:
   ```sql
   DO $$ BEGIN CREATE TABLESPACE test_tblspc LOCATION '/tmp/test_tblspc'; END $$;
   -- Expected error: ERROR: CREATE TABLESPACE cannot be executed inside a transaction block
   ```

2. **Run current test suite**:
   ```bash
   python3 -m unittest discover -s /mnt/d/Projetos/TR069-181/postgres -p "test_*.py" -v
   ```

3. **Inspect trigger lines**:
   ```bash
   sed -n '120,150p' /mnt/d/Projetos/TR069-181/postgres/init.sql
   ```
