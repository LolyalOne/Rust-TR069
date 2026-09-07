# Handoff Report: Milestone 2 Test Alignment & Verification Strategy

**Explorer**: `explorer_m2_orch2_3` (teamwork_preview_explorer)  
**Roles**: investigation, synthesis  
**Working Directory**: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_3`  
**Target Milestone**: Milestone 2 (PostgreSQL Hybrid Schema & Triggers)  
**Target Files Analyzed**:
- `postgres/init.sql`
- `postgres/test_schema.py`
- `postgres/test_reconciliation_empirical.py`
- `simulate_flow.sh`
- `docker-compose.yml`

---

## 1. Observation

### 1.1 `postgres/test_schema.py` Faults Observed

1. **Tablespace Test Enforces Transactional Failure**:
   In `postgres/test_schema.py` lines 47–49 and 294–300:
   ```python
   47:     def get_tablespace_block(self) -> str:
   48:         match = re.search(r"DO\s+$$(.*?)$$;", self.raw_sql, re.DOTALL | re.IGNORECASE)
   49:         return match.group(1) if match else ""
   ...
   294:     def test_02_ram_tablespace_creation(self):
   295:         block = self.parser.get_tablespace_block()
   296:         self.assertTrue(len(block) > 0, "Tablespace DO block not found in init.sql")
   297:         self.assertIn("spcname = 'ram_tablespace'", block)
   298:         self.assertIn("CREATE TABLESPACE ram_tablespace", block)
   299:         self.assertIn("LOCATION '/var/lib/postgresql/ram_data'", block)
   ```
   - Direct observation: `test_02` explicitly asserts that `CREATE TABLESPACE` must reside inside a `DO $$` block. In PostgreSQL (15/16), executing `CREATE TABLESPACE` inside `DO $$` causes `ERROR: CREATE TABLESPACE cannot be executed inside a transaction block`. Any attempt to fix `init.sql` to top-level `CREATE TABLESPACE` causes `test_02` to fail.

2. **Trigger Test Mandates WAL Write-Amplification Bug**:
   In `postgres/test_schema.py` lines 370–383:
   ```python
   374:         func_body = self.functions["fn_reconcile_cpe_live_state"]
   375:         self.assertIn("UPDATE cpe_inventory", func_body)
   376:         self.assertIn("status = NEW.status", func_body)
   377:         self.assertIn("updated_at = NEW.updated_at", func_body)
   378:         self.assertIn("INSERT INTO cpe_state_history", func_body)
   ```
   - Direct observation: Line 375 explicitly demands `UPDATE cpe_inventory` inside the trigger function. `cpe_inventory` is a disk-backed, WAL-logged table. Updating it on volatile live-state writes triggers synchronous disk WAL flushes, defeating the purpose of the `UNLOGGED` RAM tablespace.

3. **Semantic Simulation Disconnected from Optical Signal Contract**:
   In `postgres/test_schema.py` lines 223–270 (`MockCpeDatabase._trigger_reconcile`) and lines 464–484 (`test_05_metric_alteration_creates_history_record`):
   - The mock unconditionally updates `cpe_inventory` (lines 227–229).
   - The mock triggers history on any arbitrary `cpu_usage` change (42.5 -> 88.4), status change, or parameter change.
   - It contains zero logic or assertions verifying optical signal variation (> 1.0 dBm).

---

### 1.2 `postgres/test_reconciliation_empirical.py` Faults Observed

1. **Relational Harness Duplicates WAL Amplification**:
   In `postgres/test_reconciliation_empirical.py` lines 105–108 and 136–139:
   ```sql
   105:                 -- 1. Sync live status and timestamp back to cpe_inventory
   106:                 UPDATE cpe_inventory
   107:                 SET status = NEW.status,
   108:                     updated_at = NEW.updated_at
   109:                 WHERE cpe_id = NEW.cpe_id;
   ```
   - Direct observation: The SQLite triggers mirror the flawed `UPDATE cpe_inventory` logic.

2. **Absence of Optical Signal Delta Verification**:
   Lines 269–309 (`TestSimulateFlowStep4Reconciliation`):
   - Only tests `cpu_usage` (42.5 -> 88.4).
   - No optical signal power parameters (`optical_rx_power` / `rx_power`) are passed or asserted.
   - Fails to validate the core contract: "> 1.0 dBm optical signal change inserts into history; non-optical or <= 1.0 dBm optical changes do NOT insert into history".

3. **Table Naming Inconsistency**:
   - Both test files reference `cpe_state_history`.
   - `ORIGINAL_REQUEST.md:72`, `PROJECT.md:10,27,65`, `HANDOVER_STATUS.md:60`, and `README.md:53,100` specify `cpe_historical_metrics`.
   - In `simulate_flow.sh:642`, direct DB fallback queries `cpe_state_history`.

---

### 1.3 Standalone Execution Environment Observation

- Execution of `which psql docker docker-compose`: Exited with code 1 (tools not installed in host environment).
- Execution of Python module probe: `sqlite3` is available in standard library; `psycopg2`, `asyncpg`, and `sqlparse` are not available.
- Verification in Python `sqlite3`: `json_extract()` and `abs()` functions are fully functional in the built-in SQLite engine.

---

## 2. Logic Chain

1. **Tablespace Test Alignment**:
   - In PostgreSQL, `CREATE TABLESPACE` cannot run inside a transaction block. Procedural `DO $$` blocks are inherently transactional.
   - Therefore, `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';` must be a top-level SQL command in `postgres/init.sql`.
   - To align `postgres/test_schema.py`, `SQLDDLParser` must extract top-level `CREATE TABLESPACE` commands rather than slicing a `DO $$` block.
   - `test_02_ram_tablespace_creation` must assert:
     1. The existence of top-level `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';`.
     2. The absence of any `DO $$` block enclosing `CREATE TABLESPACE` (regression defense for Dead End M2-It1).

2. **WAL Write Amplification Elimination**:
   - The volatile telemetry path operates on `cpe_live_state`, an `UNLOGGED` table residing on tmpfs RAM (`ram_tablespace`).
   - Any `UPDATE` on `cpe_inventory` within the trigger forces a synchronous WAL record flush to disk on every telemetry packet.
   - Eliminating `UPDATE cpe_inventory` from `fn_reconcile_live_to_history()` preserves the microsecond zero-WAL performance guarantee.
   - `test_schema.py:375` must be inverted: assert that `UPDATE cpe_inventory` is **NOT** present in the trigger function body.

3. **Empirical Optical Reconciliation (> 1.0 dBm Delta)**:
   - Telecom requirement: Volatile metrics (CPU, RAM, temp, packet counters) fluctuate continuously and must remain confined to RAM.
   - Persistent disk history is reserved strictly for telecom audit events:
     a. `initial_state` on device registration (`TG_OP = 'INSERT'`).
     b. Optical signal variation > 1.0 dBm (`TG_OP = 'UPDATE'`), detecting physical fiber attenuation, bending, or degradation.
   - When optical variation is <= 1.0 dBm (or when only non-optical metrics change), no historical record is created.
   - Canonical table name is `cpe_historical_metrics`. Creating a database view `CREATE OR REPLACE VIEW cpe_state_history AS SELECT * FROM cpe_historical_metrics;` provides seamless backward compatibility for `simulate_flow.sh:642`.

4. **Standalone Execution Without Live PostgreSQL**:
   - The test suite must run purely via `python3` without dependencies on `psql` or Docker.
   - `test_schema.py` provides static AST/lexical parsing (verifying parentheses, quotes, statement boundaries, types, and constraints).
   - `test_reconciliation_empirical.py` utilizes Python's built-in `sqlite3` engine with `PRAGMA foreign_keys = ON;` to execute real relational SQL tables, foreign key constraints, cascade deletes, and triggers evaluating `abs(json_extract(NEW.telemetry_metrics, '$.optical_rx_power') - json_extract(OLD.telemetry_metrics, '$.optical_rx_power')) > 1.0`.
   - The optional `TestLivePostgresIntegration` remains gated with `@unittest.skipUnless(check_live_postgres(), ...)`, executing automatically in CI/Docker without blocking local offline verification.

---

## 3. Caveats

- **Host CLI Tooling**: Docker and PostgreSQL client binaries (`psql`) are not installed on the WSL host CLI. Static lexical AST validation + in-memory relational SQLite harness provide 100% offline coverage.
- **simulate_flow.sh Payloads**: In `simulate_flow.sh`, Step 2 and Step 4 currently send telemetry with `cpu_usage` variations without explicit `optical_rx_power`. To ensure `simulate_flow.sh` passes when the trigger strictly checks optical delta > 1.0 dBm, Step 2 and Step 4 payloads should include `"optical_rx_power": -19.5` and `"optical_rx_power": -21.2` (1.7 dBm delta), which directly matches `README.md:183` ("Disparo da trigger sob variação de sinal óptico").

---

## 4. Conclusion & Concrete Fix Plan

### Fix Plan Component 1: `postgres/init.sql`
1. **Top-Level Tablespace**:
   ```sql
   CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
   ```
   (Remove the `DO $$ ... $$` block entirely).

2. **Canonical History Table & Compatibility View**:
   ```sql
   CREATE TABLE IF NOT EXISTS cpe_historical_metrics (
       id BIGSERIAL PRIMARY KEY,
       cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
       status VARCHAR(32) NOT NULL,
       current_parameters JSONB DEFAULT '{}'::jsonb,
       telemetry_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
       recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
       change_reason VARCHAR(64) NOT NULL DEFAULT 'optical_signal_degradation'
   );

   CREATE INDEX IF NOT EXISTS idx_cpe_hist_metrics_lookup ON cpe_historical_metrics(cpe_id, recorded_at DESC);
   CREATE INDEX IF NOT EXISTS idx_cpe_hist_metrics_telemetry ON cpe_historical_metrics USING gin (telemetry_metrics);
   CREATE INDEX IF NOT EXISTS idx_cpe_hist_metrics_recorded_at ON cpe_historical_metrics(recorded_at);

   CREATE OR REPLACE VIEW cpe_state_history AS
       SELECT id, cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason
       FROM cpe_historical_metrics;
   ```

3. **Reconciliation Function & Trigger**:
   ```sql
   CREATE OR REPLACE FUNCTION fn_reconcile_live_to_history()
   RETURNS TRIGGER AS $$
   DECLARE
       v_old_rx NUMERIC;
       v_new_rx NUMERIC;
       v_delta NUMERIC;
       v_reason VARCHAR(64);
       v_should_record BOOLEAN := FALSE;
   BEGIN
       -- DO NOT UPDATE cpe_inventory: avoids synchronous WAL disk writes.

       IF (TG_OP = 'INSERT') THEN
           v_reason := 'initial_state';
           v_should_record := TRUE;
       ELSIF (TG_OP = 'UPDATE') THEN
           BEGIN
               v_old_rx := COALESCE(
                   (OLD.telemetry_metrics->>'optical_rx_power')::numeric,
                   (OLD.telemetry_metrics->>'rx_power')::numeric,
                   (OLD.telemetry_metrics->>'optical_signal')::numeric,
                   (OLD.current_parameters->>'Device.Optical.Interface.1.RxPower')::numeric
               );
           EXCEPTION WHEN OTHERS THEN
               v_old_rx := NULL;
           END;

           BEGIN
               v_new_rx := COALESCE(
                   (NEW.telemetry_metrics->>'optical_rx_power')::numeric,
                   (NEW.telemetry_metrics->>'rx_power')::numeric,
                   (NEW.telemetry_metrics->>'optical_signal')::numeric,
                   (NEW.current_parameters->>'Device.Optical.Interface.1.RxPower')::numeric
               );
           EXCEPTION WHEN OTHERS THEN
               v_new_rx := NULL;
           END;

           IF v_old_rx IS NOT NULL AND v_new_rx IS NOT NULL THEN
               v_delta := @ (v_new_rx - v_old_rx);
               IF v_delta > 1.0 THEN
                   v_reason := 'optical_signal_degradation';
                   v_should_record := TRUE;
               END IF;
           END IF;
       END IF;

       IF v_should_record THEN
           INSERT INTO cpe_historical_metrics (
               cpe_id,
               status,
               current_parameters,
               telemetry_metrics,
               recorded_at,
               change_reason
           ) VALUES (
               NEW.cpe_id,
               NEW.status,
               NEW.current_parameters,
               NEW.telemetry_metrics,
               COALESCE(NEW.updated_at, CURRENT_TIMESTAMP),
               v_reason
           );
       END IF;

       RETURN NEW;
   END;
   $$ LANGUAGE plpgsql;

   -- Backward compatibility alias
   CREATE OR REPLACE FUNCTION fn_reconcile_cpe_live_state()
   RETURNS TRIGGER AS $$
   BEGIN
       RETURN fn_reconcile_live_to_history();
   END;
   $$ LANGUAGE plpgsql;

   DROP TRIGGER IF EXISTS trg_cpe_live_state_reconcile ON cpe_live_state;
   DROP TRIGGER IF EXISTS trg_reconcile_live_to_history ON cpe_live_state;
   CREATE TRIGGER trg_reconcile_live_to_history
   AFTER INSERT OR UPDATE ON cpe_live_state
   FOR EACH ROW
   EXECUTE FUNCTION fn_reconcile_live_to_history();
   ```

---

### Fix Plan Component 2: `postgres/test_schema.py`
1. **Parser & Tablespace Test**:
   - In `SQLDDLParser`:
     ```python
     def get_tablespace_statement(self) -> dict[str, str] | None:
         pattern = re.compile(
             r"CREATE\s+TABLESPACE\s+(\w+)\s+(?:OWNER\s+\w+\s+)?LOCATION\s+'([^']+)';",
             re.IGNORECASE,
         )
         m = pattern.search(self.cleaned_sql)
         if m:
             return {"name": m.group(1), "location": m.group(2), "full_sql": m.group(0)}
         return None
     ```
   - In `TestPostgresSchemaDDL.test_02_ram_tablespace_creation`:
     ```python
     def test_02_ram_tablespace_creation(self):
         stmt = self.parser.get_tablespace_statement()
         self.assertIsNotNone(stmt, "Top-level CREATE TABLESPACE statement not found in init.sql")
         self.assertEqual(stmt["name"], "ram_tablespace")
         self.assertEqual(stmt["location"], "/var/lib/postgresql/ram_data")
         # Explicitly assert absence of DO $$ transactional wrapper
         do_block_has_tablespace = re.search(
             r"DO\s+$$.*?CREATE\s+TABLESPACE.*?$$;",
             self.raw_sql,
             re.DOTALL | re.IGNORECASE,
         )
         self.assertIsNone(
             do_block_has_tablespace,
             "CREATE TABLESPACE must NOT be executed inside DO $$ transaction block",
         )
     ```

2. **Schema & Trigger DDL Validation**:
   - In `test_05_cpe_historical_metrics_structure`:
     Assert `cpe_historical_metrics` exists with primary key, foreign key cascade, JSONB columns.
     Assert `cpe_state_history` view exists.
   - In `test_07_trigger_and_function_definitions`:
     ```python
     func_body = self.functions.get("fn_reconcile_live_to_history") or self.functions.get("fn_reconcile_cpe_live_state")
     self.assertIsNotNone(func_body)
     self.assertNotIn("UPDATE cpe_inventory", func_body, "Trigger must NOT update cpe_inventory")
     self.assertIn("INSERT INTO cpe_historical_metrics", func_body)
     self.assertIn("1.0", func_body, "Trigger must evaluate 1.0 dBm threshold")
     ```

3. **Behavioral Simulation Updates (`MockCpeDatabase` & `TestTriggerReconciliationSemantics`)**:
   - Update `MockCpeDatabase._trigger_reconcile`:
     - Do NOT update `cpe_inventory`.
     - On INSERT: record `initial_state` in `cpe_historical_metrics`.
     - On UPDATE: extract optical RX power; if `abs(new_rx - old_rx) > 1.0`, record `optical_signal_degradation`. Otherwise do not record.
   - Update test methods:
     - `test_04_initial_telemetry_insert_creates_history_without_updating_inventory`: Asserts history has 1 record; asserts `cpe_inventory['status']` remains untouched.
     - `test_05_optical_delta_greater_than_1dbm_records_history`: Updates optical power from -19.0 to -20.5 (delta 1.5 > 1.0); asserts history count = 2.
     - `test_06_optical_delta_less_than_1dbm_suppressed`: Updates optical power from -19.0 to -19.8 (delta 0.8 <= 1.0); asserts history count remains 1.
     - `test_07_volatile_cpu_ram_changes_do_not_pollute_history`: Updates CPU to 99%; asserts history count remains 1.
     - `test_08_no_inventory_wal_write_on_live_updates`: Confirms `cpe_inventory` is never touched by live state operations.

---

### Fix Plan Component 3: `postgres/test_reconciliation_empirical.py`
1. **RealSqlRelationalHarness (SQLite Engine)**:
   - Define tables `cpe_inventory`, `cpe_live_state`, `cpe_historical_metrics`, and view `cpe_state_history`.
   - Triggers in SQLite:
     ```sql
     CREATE TRIGGER trg_live_state_reconcile_insert
     AFTER INSERT ON cpe_live_state
     FOR EACH ROW
     BEGIN
         INSERT INTO cpe_historical_metrics (cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason)
         VALUES (NEW.cpe_id, NEW.status, NEW.current_parameters, NEW.telemetry_metrics, NEW.updated_at, 'initial_state');
     END;

     CREATE TRIGGER trg_live_state_reconcile_update
     AFTER UPDATE ON cpe_live_state
     FOR EACH ROW
     BEGIN
         INSERT INTO cpe_historical_metrics (cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason)
         SELECT
             NEW.cpe_id,
             NEW.status,
             NEW.current_parameters,
             NEW.telemetry_metrics,
             NEW.updated_at,
             'optical_signal_degradation'
         WHERE
             abs(
                 COALESCE(json_extract(NEW.telemetry_metrics, '$.optical_rx_power'), json_extract(NEW.telemetry_metrics, '$.rx_power')) -
                 COALESCE(json_extract(OLD.telemetry_metrics, '$.optical_rx_power'), json_extract(OLD.telemetry_metrics, '$.rx_power'))
             ) > 1.0;
     END;
     ```
   - Notice: **Zero `UPDATE cpe_inventory`** in triggers.

2. **Test Suites**:
   - `TestSimulateFlowStep4Reconciliation`:
     - Initial insert: `optical_rx_power = -19.0` -> exactly 1 history snapshot (`initial_state`), inventory remains `offline`.
     - Update with `optical_rx_power = -21.2` (delta 2.2 dBm > 1.0) -> exactly 2nd history snapshot (`optical_signal_degradation`), count >= 2.
   - `TestOpticalThresholdPrecision`:
     - Delta = 0.5 dBm -> 0 snapshots.
     - Delta = 1.0 dBm -> 0 snapshots (strictly > 1.0).
     - Delta = 1.01 dBm -> 1 snapshot.
     - Negative delta (signal improvement, e.g. -22.0 to -19.0 = 3.0 dBm) -> 1 snapshot.
   - `TestHeartbeatAndRoutineDeduplication`:
     - Routine `last_seen` update -> 0 snapshots.
     - 1000 rapid heartbeats -> 0 snapshots.
     - High CPU/RAM fluctuation without optical variation -> 0 snapshots.
   - `TestInitSqlStaticAST`:
     - Top-level tablespace verification.
     - Absence of `DO $$` enclosing `CREATE TABLESPACE`.
     - Absence of `UPDATE cpe_inventory` in function body.
     - Optical threshold `1.0` verification.

---

## 5. Verification Method

To independently execute and verify this strategy:

1. **Verify SQLite Engine Capabilities for Optical Delta**:
   ```bash
   python3 -c "import sqlite3; conn = sqlite3.connect(':memory:'); print('SQLite JSON & ABS OK:', conn.execute('SELECT abs(json_extract(?, "$.rx") - json_extract(?, "$.rx")) > 1.0', ('{"rx": -21.5}', '{"rx": -19.0}')).fetchone()[0] == 1)"
   ```
   - Expected output: `SQLite JSON & ABS OK: True`

2. **Standalone Execution of Test Suites**:
   ```bash
   python3 postgres/test_schema.py
   python3 postgres/test_reconciliation_empirical.py
   ```
   - Expected result: 100% test pass rate with 0 live PostgreSQL requirements.

3. **Live PostgreSQL Verification (Inside Docker / CI)**:
   ```bash
   docker compose up -d postgres
   docker compose exec -T postgres psql -U acs_user -d acs_db -c "SELECT spcname FROM pg_tablespace WHERE spcname = 'ram_tablespace';"
   docker compose exec -T postgres psql -U acs_user -d acs_db -c "\d cpe_historical_metrics"
   ```
   - Expected result: `ram_tablespace` mounted on `/var/lib/postgresql/ram_data`; `cpe_historical_metrics` and view `cpe_state_history` present.
