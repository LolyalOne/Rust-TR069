-- ============================================================================
-- TR-369 / USP ACS Hybrid Database Schema
-- Architecture: PostgreSQL 15+ (Persistent + In-RAM Unlogged tmpfs)
-- Milestone: M2 - Hybrid Schema & Reconciliation Triggers
-- ============================================================================

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ----------------------------------------------------------------------------
-- 1. Tablespace Creation on RAM tmpfs volume
-- ----------------------------------------------------------------------------
-- In Docker Compose, tmpfs is mounted at /var/lib/postgresql/ram_data with uid=70, gid=70.
-- Placing unlogged tables in this tablespace guarantees zero WAL writes and
-- microsecond volatile access in RAM.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') THEN
        CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
    END IF;
END $$;

-- ----------------------------------------------------------------------------
-- 2. Persistent CPE Inventory Table
-- ----------------------------------------------------------------------------
-- Stores authoritative device registration records, manufacturer metadata,
-- hardware/software versions, and primary connectivity status.
CREATE TABLE IF NOT EXISTS cpe_inventory (
    cpe_id VARCHAR(128) PRIMARY KEY,
    serial_number VARCHAR(64) UNIQUE NOT NULL,
    manufacturer VARCHAR(64) NOT NULL,
    model VARCHAR(64) NOT NULL,
    oui VARCHAR(6),
    product_class VARCHAR(64),
    hardware_version VARCHAR(64),
    software_version VARCHAR(64),
    description TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'offline',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cpe_inventory_mfg_model ON cpe_inventory(manufacturer, model);
CREATE INDEX IF NOT EXISTS idx_cpe_inventory_status ON cpe_inventory(status);

-- ----------------------------------------------------------------------------
-- 3. In-RAM Unlogged Live State Table (ram_tablespace)
-- ----------------------------------------------------------------------------
-- Microsecond volatile state storage for real-time telemetry metrics and parameters.
-- UNLOGGED bypasses Write-Ahead Logging (WAL) to minimize disk I/O and latency.
-- Lives on the ram_tablespace (tmpfs).
CREATE UNLOGGED TABLE IF NOT EXISTS cpe_live_state (
    cpe_id VARCHAR(128) PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
    endpoint_id VARCHAR(256),
    current_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    telemetry_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'offline',
    ip_address VARCHAR(64),
    firmware_version VARCHAR(64),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
) TABLESPACE ram_tablespace;

CREATE INDEX IF NOT EXISTS idx_cpe_live_params ON cpe_live_state USING gin (current_parameters);
CREATE INDEX IF NOT EXISTS idx_cpe_live_telemetry ON cpe_live_state USING gin (telemetry_metrics);
CREATE INDEX IF NOT EXISTS idx_cpe_live_status ON cpe_live_state(status);
CREATE INDEX IF NOT EXISTS idx_cpe_live_last_seen ON cpe_live_state(last_seen);

-- ----------------------------------------------------------------------------
-- 4. Persistent Historical State Table
-- ----------------------------------------------------------------------------
-- Durable audit log and time-series telemetry archive. Populated automatically
-- by the PL/pgSQL reconciliation trigger upon state or metric modifications.
CREATE TABLE IF NOT EXISTS cpe_state_history (
    id BIGSERIAL PRIMARY KEY,
    cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL,
    current_parameters JSONB DEFAULT '{}'::jsonb,
    telemetry_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    change_reason VARCHAR(64) NOT NULL DEFAULT 'telemetry_update'
);

CREATE INDEX IF NOT EXISTS idx_cpe_history_lookup ON cpe_state_history(cpe_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_cpe_history_telemetry ON cpe_state_history USING gin (telemetry_metrics);
CREATE INDEX IF NOT EXISTS idx_cpe_history_recorded_at ON cpe_state_history(recorded_at);

-- ----------------------------------------------------------------------------
-- 5. Updated_At Helper Functions and Triggers
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cpe_inventory_updated_at ON cpe_inventory;
CREATE TRIGGER trg_cpe_inventory_updated_at
BEFORE UPDATE ON cpe_inventory
FOR EACH ROW
EXECUTE FUNCTION fn_set_updated_at();

DROP TRIGGER IF EXISTS trg_cpe_live_state_updated_at ON cpe_live_state;
CREATE TRIGGER trg_cpe_live_state_updated_at
BEFORE UPDATE ON cpe_live_state
FOR EACH ROW
EXECUTE FUNCTION fn_set_updated_at();

-- ----------------------------------------------------------------------------
-- 6. State Reconciliation Function and Trigger
-- ----------------------------------------------------------------------------
-- Automatically reconciles volatile in-RAM changes to persistent storage:
--   1. Synchronizes status and updated_at to cpe_inventory.
--   2. Detects metric alterations, status changes, and parameter updates.
--   3. Snapshots meaningful transitions into cpe_state_history with change_reason.
CREATE OR REPLACE FUNCTION fn_reconcile_cpe_live_state()
RETURNS TRIGGER AS $$
DECLARE
    v_reason VARCHAR(64);
    v_should_record BOOLEAN := FALSE;
BEGIN
    -- 1. Sync live status and timestamp back to cpe_inventory
    UPDATE cpe_inventory
    SET status = NEW.status,
        updated_at = NEW.updated_at
    WHERE cpe_id = NEW.cpe_id;

    -- 2. Detect transition type and determine if historical snapshot is required
    IF (TG_OP = 'INSERT') THEN
        v_reason := 'initial_state';
        v_should_record := TRUE;
    ELSIF (TG_OP = 'UPDATE') THEN
        IF (OLD.status IS DISTINCT FROM NEW.status) AND (OLD.telemetry_metrics IS DISTINCT FROM NEW.telemetry_metrics) THEN
            v_reason := 'status_and_metrics_changed';
            v_should_record := TRUE;
        ELSIF (OLD.status IS DISTINCT FROM NEW.status) THEN
            v_reason := 'status_changed';
            v_should_record := TRUE;
        ELSIF (OLD.telemetry_metrics IS DISTINCT FROM NEW.telemetry_metrics) THEN
            v_reason := 'telemetry_metrics_changed';
            v_should_record := TRUE;
        ELSIF (OLD.current_parameters IS DISTINCT FROM NEW.current_parameters) THEN
            v_reason := 'parameters_changed';
            v_should_record := TRUE;
        END IF;
    END IF;

    -- 3. Record snapshot in persistent history
    IF v_should_record THEN
        INSERT INTO cpe_state_history (
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

DROP TRIGGER IF EXISTS trg_cpe_live_state_reconcile ON cpe_live_state;
CREATE TRIGGER trg_cpe_live_state_reconcile
AFTER INSERT OR UPDATE ON cpe_live_state
FOR EACH ROW
EXECUTE FUNCTION fn_reconcile_cpe_live_state();
