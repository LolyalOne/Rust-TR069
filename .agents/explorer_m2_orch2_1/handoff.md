# Investigation & Fix Strategy Report: Milestone 2 (PostgreSQL Hybrid Schema & Reconciliation Triggers)

**Explorer**: `explorer_m2_orch2_1` (teamwork_preview_explorer)  
**Parent Caller ID**: `72558cd4-b522-4129-816f-63bb0c581dfa` (`parent`)  
**Working Directory**: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_orch2_1`  
**Timestamp**: 2026-09-07T06:06:00Z  
**Target Milestone**: Milestone 2 — Hybrid PostgreSQL Architecture & Triggers  

---

## 1. Observation

### 1.1 Tablespace Transactional Incompatibility in `postgres/init.sql`
In `postgres/init.sql` lines 17–22:
```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') THEN
        CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
    END IF;
END $$;
```
Direct observation from PostgreSQL Engine specification:
- Official PostgreSQL documentation states: `"CREATE TABLESPACE cannot be executed inside a transaction block."`
- PostgreSQL anonymous procedural blocks (`DO $$ ... $$`) execute strictly within a transactional context.
- When PostgreSQL container initializes via Docker Compose (`docker-compose.yml:5`), `/docker-entrypoint-initdb.d/init.sql` is invoked with `psql -v ON_ERROR_STOP=1`.
- The engine raises: `ERROR: CREATE TABLESPACE cannot be executed inside a transaction block`, halting startup and leaving the database cluster completely uninitialized.

### 1.2 Write Amplification & Schema Flaws in Trigger Function
In `postgres/init.sql` lines 119–178:
- The trigger function is named `fn_reconcile_cpe_live_state()` and trigger `trg_cpe_live_state_reconcile`.
- Lines 126–130 execute an unconditional update on `cpe_inventory`:
  ```sql
  UPDATE cpe_inventory
  SET status = NEW.status,
      updated_at = NEW.updated_at
  WHERE cpe_id = NEW.cpe_id;
  ```
  `cpe_inventory` is a disk-backed, WAL-logged table. Every volatile telemetry packet or heartbeat written to `cpe_live_state` (an `UNLOGGED` RAM table) forces an immediate synchronous WAL disk write on `cpe_inventory`, defeating the zero-disk-write objective of having `ram_tablespace` in `tmpfs`.
- The historical table in `init.sql:75` is named `cpe_state_history`, whereas `ORIGINAL_REQUEST.md` (lines 69–73), `PROJECT.md` (Features 9 & 10), `README.md` (lines 53, 100), and `HANDOVER_STATUS.md` (lines 58–60) specify:
  - Table name: `cpe_historical_metrics`
  - Trigger function name: `reconcile_live_to_history`
  - Trigger condition: strictly when optical signal variation is > 1.0 dBm (`|NEW - OLD| > 1.0 dBm`), checking `NEW.telemetry_metrics->>'rx_optical_power'`, without performing an `UPDATE` on `cpe_inventory`.

### 1.3 Masking and Inconsistencies in Test Suites
1. **`postgres/test_schema.py`**:
   - Lines 48–49 & 294–300: `test_02_ram_tablespace_creation` asserts the presence of the broken `DO $$` block:
     ```python
     block = self.parser.get_tablespace_block()
     self.assertTrue(len(block) > 0, "Tablespace DO block not found in init.sql")
     ```
     This masked the invalid PostgreSQL syntax and prevented detection.
   - Lines 115–274: `MockCpeDatabase` does not test `init.sql` DDL or actual PostgreSQL behavior, but rather simulates in-memory dictionary transitions that replicated the invalid `UPDATE cpe_inventory` and non-optical transition reasons.
   - Line 375: `test_07_trigger_and_function_definitions` explicitly asserts `self.assertIn("UPDATE cpe_inventory", func_body)` — certifying the write-amplification bug as required behavior.
2. **`postgres/test_reconciliation_empirical.py`**:
   - Lines 105–109 & 136–140: SQLite trigger harness mirrors `UPDATE cpe_inventory` and records history for `telemetry_metrics_changed` (based on `cpu_usage`) rather than optical signal variation (`rx_optical_power > 1.0 dBm`).
3. **`simulate_flow.sh` Dependencies**:
   - In `simulate_flow.sh` lines 516 & 599, the test telemetry payloads contain `cpu_usage` (42.5 -> 88.4), but omit `rx_optical_power`.
   - Line 642 queries: `SELECT count(*) FROM cpe_state_history WHERE cpe_id='${CPE_ID}';` expecting `>= 2` snapshots.
   - If `reconcile_live_to_history` strictly monitors optical variation and records to `cpe_historical_metrics`, `simulate_flow.sh` Step 4 requires `rx_optical_power` in its payload, and a compatibility view `cpe_state_history` pointing to `cpe_historical_metrics` ensures complete zero-breakage interoperability.

---

## 2. Logic Chain

1. **Tablespace Resolution**:
   - `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';` must be a standalone top-level statement executed outside any procedural `DO $$` or transactional block.
   - When PostgreSQL initializes `/docker-entrypoint-initdb.d/init.sql`, top-level statements run outside of transaction blocks with autocommit, allowing `CREATE TABLESPACE` to succeed without raising `ERROR: CREATE TABLESPACE cannot be executed inside a transaction block`.

2. **Trigger and Schema Realignment**:
   - Define primary table `cpe_historical_metrics` in `init.sql` matching `PROJECT.md` Feature 9 and `README.md`.
   - Create view `cpe_state_history AS SELECT * FROM cpe_historical_metrics;` to guarantee backward compatibility for `simulate_flow.sh` line 642 and existing query endpoints.
   - In `reconcile_live_to_history()`:
     - Remove `UPDATE cpe_inventory` completely. `cpe_inventory` is managed strictly via API CRUD; telemetry writes in RAM produce ZERO disk WAL writes.
     - Extract `rx_optical_power` (with fallbacks to `optical_rx_power`, `rx_power`) from `NEW.telemetry_metrics` and `OLD.telemetry_metrics`.
     - On `TG_OP = 'INSERT'`: if `rx_optical_power` is present, record baseline snapshot (`initial_state`).
     - On `TG_OP = 'UPDATE'`:
       - If previous was NULL and current has optical power, record baseline snapshot (`initial_state`).
       - If both previous and current have optical power, calculate `v_delta := abs(NEW_rx - OLD_rx)`. If `v_delta > 1.0`, insert into `cpe_historical_metrics` with `change_reason := 'optical_signal_variation'`.
       - If `v_delta <= 1.0` or only routine parameters/CPU/memory/heartbeats changed: DO NOT INSERT.
   - Alias `fn_reconcile_cpe_live_state()` to execute `reconcile_live_to_history()` to prevent breakage across any legacy caller.

3. **Verification Harness Alignment**:
   - `test_schema.py`:
     - Update parser to assert `CREATE TABLESPACE` is a top-level statement and assert `has_tablespace_in_do_block() == False`.
     - Update table structure tests to validate `cpe_historical_metrics` and view `cpe_state_history`.
     - Update trigger assertions: assert `"UPDATE cpe_inventory" not in func_body`, assert `rx_optical_power`, `1.0`, and `optical_signal_variation` are present.
     - Update `MockCpeDatabase` and `TestTriggerReconciliationSemantics` to test optical variation > 1.0 dBm, asserting that CPU/RAM/heartbeat updates do not trigger historical writes and `cpe_inventory` is never modified.
   - `test_reconciliation_empirical.py`:
     - Update `RealSqlRelationalHarness` triggers to execute the exact optical signal delta logic without `cpe_inventory` updates.
     - In Step 4 test: initial payload includes `rx_optical_power: -18.5`, altered payload has `rx_optical_power: -21.0` (delta 2.5 dBm > 1.0 dBm). Asserts record count = 2.
     - Assert routine heartbeats, CPU alterations, and optical fluctuations <= 1.0 dBm (e.g. -18.8 dBm) create zero additional history records.

---

## 3. Caveats

- **Host Docker Absence**: Docker is not installed on this specific WSL host; live container spin-up cannot be tested directly on the host CLI. However, empirical verification via SQLite 3.37.2 (with JSON extensions) directly models the SQL relational and trigger logic.
- **`simulate_flow.sh` Telemetry Payloads**: `simulate_flow.sh` was delivered in Milestone 1 with generic `cpu_usage` payloads. While M2 focuses on database schema and unit tests, `simulate_flow.sh` will need `rx_optical_power` added to its Step 2 & Step 4 payloads (or included in the test runner) for Milestone 5 acceptance.

---

## 4. Conclusion & Concrete Fix Plan

### Action Item 1: Proposed Changes to `postgres/init.sql`

#### 1.1 Tablespace Section (Lines 11–23)
```sql
-- ----------------------------------------------------------------------------
-- 1. Tablespace Creation on RAM tmpfs volume
-- ----------------------------------------------------------------------------
-- In Docker Compose, tmpfs is mounted at /var/lib/postgresql/ram_data with uid=70, gid=70.
-- Placing unlogged tables in this tablespace guarantees zero WAL writes and
-- microsecond volatile access in RAM.
-- Note: CREATE TABLESPACE cannot be executed inside a transaction block (DO $$).
-- It must be executed as a top-level command.
CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
```

#### 1.2 Historical Table Section (Lines 71–89)
```sql
-- ----------------------------------------------------------------------------
-- 4. Persistent Historical Metrics Table
-- ----------------------------------------------------------------------------
-- Durable audit log and time-series telemetry archive. Populated automatically
-- by the PL/pgSQL reconciliation trigger strictly when optical signal variation
-- exceeds 1.0 dBm (|NEW - OLD| > 1.0 dBm).
CREATE TABLE IF NOT EXISTS cpe_historical_metrics (
    id BIGSERIAL PRIMARY KEY,
    cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL,
    current_parameters JSONB DEFAULT '{}'::jsonb,
    telemetry_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    change_reason VARCHAR(64) NOT NULL DEFAULT 'optical_signal_variation'
);

CREATE INDEX IF NOT EXISTS idx_cpe_history_lookup ON cpe_historical_metrics(cpe_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_cpe_history_telemetry ON cpe_historical_metrics USING gin (telemetry_metrics);
CREATE INDEX IF NOT EXISTS idx_cpe_history_recorded_at ON cpe_historical_metrics(recorded_at);

-- Backward compatibility view for cpe_state_history
CREATE OR REPLACE VIEW cpe_state_history AS
SELECT id, cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason
FROM cpe_historical_metrics;
```

#### 1.3 Reconciliation Trigger Function (Lines 113–179)
```sql
-- ----------------------------------------------------------------------------
-- 6. State Reconciliation Function and Trigger
-- ----------------------------------------------------------------------------
-- Automatically reconciles volatile in-RAM changes to persistent storage strictly
-- when optical signal variation exceeds 1.0 dBm (|NEW - OLD| > 1.0 dBm).
-- Zero disk WAL overhead on routine heartbeats and metric updates.
-- Does NOT perform UPDATE on cpe_inventory to prevent disk write amplification.
CREATE OR REPLACE FUNCTION reconcile_live_to_history()
RETURNS TRIGGER AS $$
DECLARE
    v_new_rx_power NUMERIC := NULL;
    v_old_rx_power NUMERIC := NULL;
    v_new_val TEXT := NULL;
    v_old_val TEXT := NULL;
    v_delta NUMERIC := 0.0;
    v_reason VARCHAR(64) := 'optical_signal_variation';
    v_should_record BOOLEAN := FALSE;
BEGIN
    -- 1. Extract optical power from NEW.telemetry_metrics
    IF NEW.telemetry_metrics IS NOT NULL THEN
        v_new_val := COALESCE(
            NEW.telemetry_metrics->>'rx_optical_power',
            NEW.telemetry_metrics->>'optical_rx_power',
            NEW.telemetry_metrics->>'rx_power',
            NEW.telemetry_metrics->>'optical_power'
        );
        IF v_new_val IS NOT NULL AND v_new_val ~ '^-?[0-9]+(\.[0-9]+)?$' THEN
            v_new_rx_power := v_new_val::numeric;
        END IF;
    END IF;

    -- 2. Extract optical power from OLD.telemetry_metrics on UPDATE
    IF TG_OP = 'UPDATE' AND OLD.telemetry_metrics IS NOT NULL THEN
        v_old_val := COALESCE(
            OLD.telemetry_metrics->>'rx_optical_power',
            OLD.telemetry_metrics->>'optical_rx_power',
            OLD.telemetry_metrics->>'rx_power',
            OLD.telemetry_metrics->>'optical_power'
        );
        IF v_old_val IS NOT NULL AND v_old_val ~ '^-?[0-9]+(\.[0-9]+)?$' THEN
            v_old_rx_power := v_old_val::numeric;
        END IF;
    END IF;

    -- 3. Evaluate optical variation conditions:
    --    - Initial baseline established on INSERT (or first optical report on UPDATE)
    --    - Historical snapshot recorded strictly when optical delta > 1.0 dBm
    IF TG_OP = 'INSERT' THEN
        IF v_new_rx_power IS NOT NULL THEN
            v_reason := 'initial_state';
            v_should_record := TRUE;
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF v_old_rx_power IS NULL AND v_new_rx_power IS NOT NULL THEN
            v_reason := 'initial_state';
            v_should_record := TRUE;
        ELSIF v_old_rx_power IS NOT NULL AND v_new_rx_power IS NOT NULL THEN
            v_delta := abs(v_new_rx_power - v_old_rx_power);
            IF v_delta > 1.0 THEN
                v_reason := 'optical_signal_variation';
                v_should_record := TRUE;
            END IF;
        END IF;
    END IF;

    -- 4. Record snapshot in persistent history table (WITHOUT updating cpe_inventory)
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
    RETURN reconcile_live_to_history();
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cpe_live_state_reconcile ON cpe_live_state;
DROP TRIGGER IF EXISTS trg_reconcile_live_to_history ON cpe_live_state;
CREATE TRIGGER trg_reconcile_live_to_history
AFTER INSERT OR UPDATE ON cpe_live_state
FOR EACH ROW
EXECUTE FUNCTION reconcile_live_to_history();
```

---

### Action Item 2: Proposed Changes to `postgres/test_schema.py`

1. **`SQLDDLParser` Updates**:
   - Add `has_tablespace_in_do_block(self) -> bool`:
     `return bool(re.search(r"DO\s+\$\$.*?CREATE\s+TABLESPACE.*?\$\$;", self.raw_sql, re.DOTALL | re.IGNORECASE))`
   - Add `get_top_level_tablespace_statement(self) -> str`:
     `match = re.search(r"^\s*CREATE\s+TABLESPACE\s+ram_tablespace\s+LOCATION\s+'([^']+)'\s*;", self.cleaned_sql, re.MULTILINE | re.IGNORECASE)`
   - Add `get_views(self) -> list[str]`:
     `re.findall(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)", self.cleaned_sql, re.IGNORECASE)`

2. **`TestPostgresSchemaDDL` Test Fixes**:
   - `test_02_ram_tablespace_creation`:
     ```python
     def test_02_ram_tablespace_creation(self):
         self.assertFalse(self.parser.has_tablespace_in_do_block(), "CREATE TABLESPACE must NOT be executed inside a DO $$ transaction block")
         tb_stmt = self.parser.get_top_level_tablespace_statement()
         self.assertIsNotNone(tb_stmt, "Top-level CREATE TABLESPACE ram_tablespace statement not found")
         self.assertEqual(tb_stmt, "/var/lib/postgresql/ram_data")
     ```
   - `test_05_cpe_historical_metrics_table_structure`:
     Verify `cpe_historical_metrics` table exists with columns `id`, `cpe_id`, `status`, `current_parameters`, `telemetry_metrics`, `recorded_at`, `change_reason`.
     Verify view `cpe_state_history` is defined.
   - `test_07_trigger_and_function_definitions`:
     ```python
     def test_07_trigger_and_function_definitions(self):
         self.assertIn("fn_set_updated_at", self.functions)
         self.assertTrue("reconcile_live_to_history" in self.functions or "fn_reconcile_cpe_live_state" in self.functions)
         func_body = self.functions.get("reconcile_live_to_history") or self.functions.get("fn_reconcile_cpe_live_state")

         # WAL write-amplification prevention: Trigger MUST NOT update cpe_inventory
         self.assertNotIn("UPDATE cpe_inventory", func_body, "Reconciliation trigger must NOT perform UPDATE on cpe_inventory")
         
         # Optical threshold validation
         self.assertIn("rx_optical_power", func_body)
         self.assertIn("1.0", func_body)
         self.assertIn("optical_signal_variation", func_body)
         self.assertIn("cpe_historical_metrics", func_body)
     ```

3. **`MockCpeDatabase` and `TestTriggerReconciliationSemantics` Updates**:
   - In `MockCpeDatabase._trigger_reconcile()`:
     - Remove `self.cpe_inventory[cpe_id]["status"] = ...` (do not touch inventory).
     - Extract `rx_optical_power` from `NEW` and `OLD`.
     - On `INSERT`: record `initial_state` if optical metric present.
     - On `UPDATE`: record `optical_signal_variation` if `abs(new_rx - old_rx) > 1.0`.
   - Update tests:
     - `test_04_initial_telemetry_insert_records_optical_baseline`: with `rx_optical_power: -18.5`, creates 1 history record, leaves inventory status unchanged.
     - `test_05_optical_variation_greater_than_1dbm_creates_history`: update with `rx_optical_power: -20.0` (delta = 1.5 > 1.0) creates 2nd history record.
     - `test_06_optical_variation_within_threshold_ignored`: update with `rx_optical_power: -20.5` (delta = 0.5 <= 1.0) does not create record (count remains 2).
     - `test_07_cpu_metric_alteration_without_optical_delta_does_not_record_history`: CPU usage updated to 88.4 without optical change does not create record (count remains 2).
     - `test_08_status_change_without_optical_delta_does_not_record_history`: status changed to rebooting does not create record and does NOT update `cpe_inventory`.

---

### Action Item 3: Proposed Changes to `postgres/test_reconciliation_empirical.py`

1. **`RealSqlRelationalHarness`**:
   - Table schema: create `cpe_historical_metrics` and `CREATE VIEW cpe_state_history AS SELECT * FROM cpe_historical_metrics;`.
   - In SQLite triggers:
     - `trg_live_state_reconcile_insert`:
       ```sql
       CREATE TRIGGER trg_live_state_reconcile_insert
       AFTER INSERT ON cpe_live_state
       FOR EACH ROW
       WHEN json_extract(NEW.telemetry_metrics, '$.rx_optical_power') IS NOT NULL
       BEGIN
           INSERT INTO cpe_historical_metrics (cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason)
           VALUES (NEW.cpe_id, NEW.status, NEW.current_parameters, NEW.telemetry_metrics, NEW.updated_at, 'initial_state');
       END;
       ```
     - `trg_live_state_reconcile_update`:
       ```sql
       CREATE TRIGGER trg_live_state_reconcile_update
       AFTER UPDATE ON cpe_live_state
       FOR EACH ROW
       WHEN json_extract(NEW.telemetry_metrics, '$.rx_optical_power') IS NOT NULL
       BEGIN
           INSERT INTO cpe_historical_metrics (cpe_id, status, current_parameters, telemetry_metrics, recorded_at, change_reason)
           SELECT
               NEW.cpe_id,
               NEW.status,
               NEW.current_parameters,
               NEW.telemetry_metrics,
               NEW.updated_at,
               CASE
                   WHEN json_extract(OLD.telemetry_metrics, '$.rx_optical_power') IS NULL THEN 'initial_state'
                   ELSE 'optical_signal_variation'
               END
           WHERE
               json_extract(OLD.telemetry_metrics, '$.rx_optical_power') IS NULL
               OR ABS(CAST(json_extract(NEW.telemetry_metrics, '$.rx_optical_power') AS REAL) - CAST(json_extract(OLD.telemetry_metrics, '$.rx_optical_power') AS REAL)) > 1.0;
       END;
       ```
     - Assert that neither trigger contains `UPDATE cpe_inventory`.

2. **Empirical Tests**:
   - In `TestSimulateFlowStep4Reconciliation.test_step4_flow_end_to_end`:
     - Step 2: payload with `rx_optical_power: -18.5` and `cpu_usage: 42.5` -> asserts 1 record (`initial_state`).
     - Step 4: payload with `rx_optical_power: -21.0` (delta = 2.5 > 1.0) and `cpu_usage: 88.4` -> asserts 2 records (`optical_signal_variation`).
     - Asserts `cpe_inventory` status was NOT touched by the trigger (remains `'offline'`).
   - In `TestHeartbeatAndRoutineDeduplication`:
     - 1000 rapid heartbeats produce zero additional records.
     - Small optical power changes <= 1.0 dBm (e.g. -18.5 -> -18.8 -> -19.2) produce zero additional records.
   - In `TestInitSqlStaticAST`:
     - Assert `CREATE TABLESPACE` is not in `DO $$`.
     - Assert `cpe_historical_metrics` is present.
     - Assert `UPDATE cpe_inventory` is absent from trigger function.

---

### Action Item 4: Alignment Recommendation for `simulate_flow.sh`
In `simulate_flow.sh`:
- Line 516 (Step 2 `TELEMETRY_PAYLOAD`): add `"rx_optical_power": -18.5`.
- Line 599 (Step 4 `MODIFIED_TELEMETRY`): add `"rx_optical_power": -21.0`.
This ensures Step 4 triggers the `reconcile_live_to_history` trigger with a delta of 2.5 dBm (> 1.0 dBm), populating `cpe_historical_metrics` (accessible via view `cpe_state_history`) with count >= 2, passing Milestone 5 without modification to the test logic.

---

## 5. Verification Method

### How to Independently Verify the Fixes:

1. **Verify Static DDL & Tablespace Syntax**:
   ```bash
   python3 -c "
   with open('postgres/init.sql') as f:
       sql = f.read()
   assert 'CREATE TABLESPACE ram_tablespace LOCATION \'/var/lib/postgresql/ram_data\';' in sql
   assert 'DO $$' not in sql.split('CREATE TABLESPACE')[0][-50:]
   assert 'UPDATE cpe_inventory' not in sql[sql.find('FUNCTION reconcile_live_to_history'):]
   print('Static DDL checks passed.')
   "
   ```

2. **Run the Schema DDL Test Suite**:
   ```bash
   python3 postgres/test_schema.py
   ```
   *Expected result*: 20 tests pass (0 failures, 0 errors, live test skipped if container not up).

3. **Run the Empirical Reconciliation Test Suite**:
   ```bash
   python3 postgres/test_reconciliation_empirical.py
   ```
   *Expected result*: All 18 tests pass with 100% success rate, confirming optical variation threshold > 1.0 dBm and zero disk write amplification.
