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
-- Note: CREATE TABLESPACE cannot be executed inside a transaction block.
-- It must be executed as a top-level command.
CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';

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
    optical_power NUMERIC(6,2),
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
--   1. Strictly triggers when optical signal variation is > 1.0 dBm (|NEW - OLD| > 1.0 dBm).
--   2. Records initial baseline snapshot when optical signal is first acquired.
--   3. Inserts into cpe_historical_metrics (accessible via cpe_state_history view).
--   4. NEVER performs UPDATE on cpe_inventory (zero WAL write amplification).
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
    -- 1. Extract optical power from NEW.telemetry_metrics or current_parameters
    IF NEW.telemetry_metrics IS NOT NULL THEN
        v_new_val := COALESCE(
            NEW.telemetry_metrics->>'rx_optical_power',
            NEW.telemetry_metrics->>'optical_power',
            NEW.telemetry_metrics->>'optical_rx_power',
            NEW.telemetry_metrics->>'rx_power'
        );
    END IF;
    IF v_new_val IS NULL AND NEW.current_parameters IS NOT NULL THEN
        v_new_val := COALESCE(
            NEW.current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel',
            NEW.current_parameters->>'Device.Optical.Interface.1.RxPower'
        );
    END IF;

    IF v_new_val IS NOT NULL AND v_new_val ~ '^-?[0-9]+(\.[0-9]+)?$' THEN
        v_new_rx_power := v_new_val::numeric;
    END IF;

    -- 2. Extract optical power from OLD on UPDATE
    IF TG_OP = 'UPDATE' THEN
        IF OLD.telemetry_metrics IS NOT NULL THEN
            v_old_val := COALESCE(
                OLD.telemetry_metrics->>'rx_optical_power',
                OLD.telemetry_metrics->>'optical_power',
                OLD.telemetry_metrics->>'optical_rx_power',
                OLD.telemetry_metrics->>'rx_power'
            );
        END IF;
        IF v_old_val IS NULL AND OLD.current_parameters IS NOT NULL THEN
            v_old_val := COALESCE(
                OLD.current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel',
                OLD.current_parameters->>'Device.Optical.Interface.1.RxPower'
            );
        END IF;

        IF v_old_val IS NOT NULL AND v_old_val ~ '^-?[0-9]+(\.[0-9]+)?$' THEN
            v_old_rx_power := v_old_val::numeric;
        END IF;
    END IF;

    -- 3. Evaluate optical variation conditions:
    -- On INSERT: record baseline snapshot if optical power is present
    IF TG_OP = 'INSERT' THEN
        IF v_new_rx_power IS NOT NULL THEN
            v_reason := 'initial_state';
            v_should_record := TRUE;
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        -- If old had no optical reading and new does, record initial optical baseline
        IF v_old_rx_power IS NULL AND v_new_rx_power IS NOT NULL THEN
            v_reason := 'initial_state';
            v_should_record := TRUE;
        ELSIF v_old_rx_power IS NOT NULL AND v_new_rx_power IS NOT NULL THEN
            v_delta := abs(v_new_rx_power - v_old_rx_power);
            -- Strictly trigger when optical signal variation is > 1.0 dBm
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
            optical_power,
            recorded_at,
            change_reason
        ) VALUES (
            NEW.cpe_id,
            NEW.status,
            NEW.current_parameters,
            NEW.telemetry_metrics,
            v_new_rx_power,
            COALESCE(NEW.updated_at, CURRENT_TIMESTAMP),
            v_reason
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Backward compatibility alias function
CREATE OR REPLACE FUNCTION fn_reconcile_cpe_live_state()
RETURNS TRIGGER AS $$
BEGIN
    RETURN reconcile_live_to_history();
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cpe_live_state_reconcile ON cpe_live_state;
DROP TRIGGER IF EXISTS reconcile_live_to_history ON cpe_live_state;
CREATE TRIGGER reconcile_live_to_history
AFTER INSERT OR UPDATE ON cpe_live_state
FOR EACH ROW
EXECUTE FUNCTION reconcile_live_to_history();
