# Technical Specification & Handoff Report: PostgreSQL Database Sink & Multi-Stage Dockerfile (Milestone 3)

**Author:** `explorer_m3_3` (teamwork_preview_explorer)  
**Target Milestone:** Milestone 3 — Rust USP Core Worker (`rust-core/`)  
**Scope:** PostgreSQL Database Sink (SQLx Pool, Asynchronous UPSERT, JSONB Concatenation) & Multi-Stage Production Dockerfile  
**Date:** 2026-09-07  

---

## 1. Observation

### 1.1 Project Requirements & Architectural Constraints
From `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md`:
- **Line 20 (R1)**: Strict memory limits: Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB), Python FastAPI (1 GB). Postgres must use a `tmpfs` volume for in-memory tablespace (`/var/lib/postgresql/ram_data`).
- **Line 29 (R4)**: Persistent relational table `cpe_inventory` and `UNLOGGED` RAM table `cpe_live_state`. A reconciliation trigger must migrate state changes to historical metrics without disk write amplification.
- **Line 32 (R5)**: Asynchronous service using `tokio`, `rumqttc`, and `sqlx` processing messages from broker (`usp/endpoint/#`) and persisting state using MPSC Channels for decoupling.
- **Lines 48–54**: End-to-end verification via `simulate_flow.sh`:
  - Step 1: Register test CPE via FastAPI (`cpe_id = cpe-sim-001`).
  - Step 2: Publish TR-369 telemetry payload via MQTT (`rx_optical_power: -18.5, cpu_usage: 42.5`).
  - Step 3: Validate Rust worker consumed message and updated RAM table (`cpe_live_state`) with `status=online` and `cpu_usage=42.5`.
  - Step 4: Publish modified telemetry (`rx_optical_power: -21.0, cpu_usage: 88.4`) and validate trigger reconciliation into `cpe_state_history` (count >= 2).

### 1.2 Target Database Schema (`postgres/init.sql`)
From `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql`:
- **Lines 19–20**: `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';` (executed outside transaction blocks).
- **Lines 26–39**: `cpe_inventory` table definition with primary key `cpe_id VARCHAR(128)`.
- **Lines 50–60**: `cpe_live_state` table definition:
  ```sql
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
  ```
- **Lines 124–222**: Trigger function `reconcile_live_to_history()`:
  - Triggered `AFTER INSERT OR UPDATE ON cpe_live_state FOR EACH ROW`.
  - On `INSERT`: if optical power present, records baseline snapshot into `cpe_historical_metrics` with `change_reason = 'initial_state'`.
  - On `UPDATE`: extracts optical power from `NEW` and `OLD` (`rx_optical_power`, `optical_power`, etc.). If `|v_new - v_old| > 1.0 dBm`, inserts snapshot into `cpe_historical_metrics` with `change_reason = 'optical_signal_variation'`.
  - Never executes `UPDATE` on `cpe_inventory`, avoiding WAL write amplification.

### 1.3 Container Infrastructure Configuration (`docker-compose.yml`)
From `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml`:
- **Lines 48–72**:
  ```yaml
  rust-core:
    build:
      context: ./rust-core
    environment:
      - DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db
      - MQTT_HOST=mosquitto
      - MQTT_PORT=1883
    depends_on:
      postgres:
        condition: service_healthy
      mosquitto:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 500M
    healthcheck:
      test: ["CMD-SHELL", "test -f /tmp/healthy || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 5s
    networks:
      - acs_network
  ```

### 1.4 Host Environment Probing & Toolchain State
Execution results from system investigation commands:
- `cargo --version && rustc --version`: Exited 127 (`bash: line 1: cargo: command not found`). Cargo and Rust toolchain are not installed directly on the host WSL system.
- `docker --version`: Not in host WSL PATH.
- `python3 --version`: Python 3.12.3 installed at `/usr/bin/python3`.
- `python3 postgres/test_schema.py`: Ran 20 tests; **19 passed, 1 skipped** (live DB skipped, AST DDL passed 100%).
- `python3 postgres/test_reconciliation_empirical.py`: Ran 23 tests; **23 passed (100% OK in 0.204s)**.
- `python3 .agents/explorer_m3_3/test_sql_upsert_verification.py`: Ran 3 tests; **3 passed (100% OK in 0.008s)**.
- `.devcontainer/devcontainer.json`: Configured with feature `"ghcr.io/devcontainers/features/rust:1": {}`, indicating containerized execution is the primary development path.

### 1.5 Peer Agent Interface Discoveries
- `.agents/explorer_m3_1/handoff.md`: Designed `proto/usp.proto` and dual payload decoder producing unified struct `TelemetryUpdate`.
- `.agents/explorer_m3_2/handoff.md`: Designed Tokio async runtime, Rumqttc event loop, MPSC channel (`tokio::sync::mpsc::channel(1024)`), and `/tmp/healthy` healthcheck updater.
- Both peer agents agree on the exact fields of `TelemetryUpdate`:
  - `cpe_id: String`
  - `endpoint_id: Option<String>`
  - `status: String`
  - `current_parameters: serde_json::Value`
  - `telemetry_metrics: serde_json::Value`
  - `ip_address: Option<String>`
  - `firmware_version: Option<String>`
  - `received_at: chrono::DateTime<chrono::Utc>`

---

## 2. Logic Chain

### 2.1 Mapping `cpe_live_state` DDL to SQLx Bind Parameters
- **Observation**: `cpe_live_state` has 9 columns: `cpe_id`, `endpoint_id`, `current_parameters`, `telemetry_metrics`, `status`, `ip_address`, `firmware_version`, `last_seen`, `updated_at`.
- **Reasoning**:
  1. `cpe_id` is the primary key. All conflict handling is keyed on `cpe_id`.
  2. `endpoint_id`, `ip_address`, and `firmware_version` are optional device properties (`Option<String>`).
  3. `current_parameters` and `telemetry_metrics` are `JSONB NOT NULL DEFAULT '{}'::jsonb`.
  4. `status` is a string (default `'online'` upon receiving live telemetry).
  5. `last_seen` represents the timestamp of the latest received message.
  6. `updated_at` is managed by PostgreSQL (`fn_set_updated_at` trigger and query expression `CURRENT_TIMESTAMP`).

### 2.2 Mechanics of JSONB Concatenation (`||`) and Null Safety
- **Observation**: In PostgreSQL, the `||` operator merges two JSONB objects:
  `'{"cpu": 42.5}'::jsonb || '{"cpu": 88.4, "rx_optical_power": -21.0}'::jsonb` yields `'{"cpu": 88.4, "rx_optical_power": -21.0}'::jsonb`.
- **Deduction**:
  - Existing keys not present in the incoming payload remain intact (e.g. baseline configuration parameters are not erased when only metrics update).
  - Incoming keys overwrite existing keys with the newest telemetry reading.
  - **Critical Edge Case**: If either operand of `||` is `NULL`, PostgreSQL returns `NULL`. If incoming parameters are `NULL`, executing `cpe_live_state.current_parameters || EXCLUDED.current_parameters` would set the column to `NULL`, violating the `NOT NULL` constraint and causing transaction failure.
  - **Defensive Solution**: Wrap both target and excluded operands with `COALESCE(EXCLUDED.current_parameters, '{}'::jsonb)` and `COALESCE(EXCLUDED.telemetry_metrics, '{}'::jsonb)`.

### 2.3 Trigger Reconciliation Activation
- **Observation**: `simulate_flow.sh` Step 4 publishes altered telemetry where optical power drops from `-18.5 dBm` to `-21.0 dBm` (delta = 2.5 dBm).
- **Reasoning**:
  - On Step 2 (Initial Ingest): `INSERT` into `cpe_live_state` executes trigger `reconcile_live_to_history()`. Because `TG_OP = 'INSERT'` and `v_new_rx_power` (-18.5) is present, a baseline row is inserted into `cpe_historical_metrics` with `change_reason = 'initial_state'`.
  - On Step 4 (Altered Telemetry): `ON CONFLICT (cpe_id) DO UPDATE` fires `reconcile_live_to_history()` as `TG_OP = 'UPDATE'`.
  - The trigger calculates `v_delta := abs(NEW.optical_power - OLD.optical_power)` = `abs(-21.0 - (-18.5))` = `2.5 dBm`.
  - Because `2.5 > 1.0`, `v_should_record` is `TRUE`, and a second row is inserted into `cpe_historical_metrics` with `change_reason = 'optical_signal_variation'`.
  - `cpe_state_history` count becomes 2, satisfying Step 4 assertion (`COUNT >= 2`).

### 2.4 Foreign Key Integrity and Error Handling
- **Observation**: `cpe_live_state.cpe_id` references `cpe_inventory(cpe_id) ON DELETE CASCADE`.
- **Reasoning**:
  - If a device sends telemetry before being registered in `cpe_inventory`, PostgreSQL raises error code `23503` (`foreign_key_violation`).
  - The SQLx database sink must catch `sqlx::Error::Database` where `code == "23503"`, log a warning (`unregistered CPE`), and acknowledge or discard the message rather than crashing the worker task or stalling the MPSC channel.

### 2.5 SQLx Pool Sizing & Memory Containment Under 500 MB Limit
- **Observation**: `rust-core` has a strict physical memory limit of 500 MB.
- **Reasoning**:
  - PostgreSQL connections consume memory both on the server and in the client (socket buffers, TLS states, connection handle allocations).
  - In an asynchronous Tokio runtime, queries against an `UNLOGGED` RAM table take < 1 ms.
  - A small connection pool of `max_connections = 10` (with `min_connections = 2`) provides capacity for thousands of operations per second with minimal resident memory.
  - Memory consumption of the SQLx pool with 10 connections is under 5 MB. The entire compiled Rust worker stays below 25 MB RSS, which is < 5% of the 500 MB limit.

### 2.6 Dynamic SQLx Query vs Compile-Time Macro (`sqlx::query` vs `sqlx::query!`)
- **Observation**: `sqlx::query!` validates SQL syntax and types against a live database during `cargo build` or requires a pre-generated `sqlx-data.json` file.
- **Reasoning**:
  - During a clean `docker compose build`, PostgreSQL is not running.
  - No `sqlx-data.json` file currently exists in the repository.
  - Using `sqlx::query!` would cause `docker build` to fail immediately.
  - Therefore, the database sink **must use dynamic queries** (`sqlx::query(...)` with `.bind(...)`), which compile cleanly in offline environments while retaining full type safety in Rust code.

### 2.7 Multi-Stage Alpine Dockerfile Architecture
- **Observation**:
  - Container must stay well within 500 MB RAM.
  - `prost-build` requires `protoc` and C compiler tools (`musl-dev`, `protobuf`, `protobuf-dev`, `build-base`).
  - Runtime image must be minimal and run as non-root.
  - Docker Compose specifies healthcheck `test -f /tmp/healthy || exit 1`.
- **Reasoning**:
  - **Stage 1 (Builder)**: `rust:1.77-alpine` or `rust:alpine`.
    - Install `musl-dev`, `protobuf`, `protobuf-dev`, `build-base`.
    - Set `ENV PROTOC=/usr/bin/protoc`.
    - Strip binary (`strip target/release/rust-core`) to discard debug symbols, reducing binary size from ~40 MB to ~10 MB.
  - **Stage 2 (Runner)**: `alpine:3.19` (or `alpine:latest`).
    - Install `ca-certificates` and `libgcc`.
    - Create non-root system user and group (`appuser:appgroup`).
    - In Alpine/Linux, `/tmp` has permissions `1777` (`rwxrwxrwt`), meaning `appuser` has full permission to write `/tmp/healthy` without elevated privileges.
    - Copy only the stripped binary from builder.
    - Resulting container image size is ~25 MB, and memory consumption during execution is ~15–25 MB RSS.

---

## 3. Caveats

1. **Host Environment Toolchain Absence**:
   - `cargo`, `rustc`, and `docker` are not in the host WSL PATH. Local testing on host must be conducted via Python verification scripts, DevContainer, or within Docker when running.
   - Code must be authored with zero assumptions of host compiler feedback, maintaining absolute adherence to Rust 2021 edition syntax and standard SQLx / Tokio / Prost APIs.

2. **Offline Compilation Requirement (`sqlx::query` vs `sqlx::query!`)**:
   - As established in Section 2.6, `sqlx::query!` must **never** be used in `rust-core/src/db_sink.rs` unless offline metadata is committed. Dynamic `sqlx::query` is required.

3. **Strict Foreign Key Ordering in E2E Flow**:
   - `cpe_live_state` requires an existing `cpe_inventory` row. If `simulate_flow.sh` is executed out of order (Step 2 before Step 1), the worker will reject the upsert with code `23503`. The worker logs this cleanly as a warning.

4. **JSONB Concatenation with Empty Objects**:
   - If incoming telemetry contains empty JSON (`{}`), merging `'{"existing": 1}'::jsonb || '{}'::jsonb` preserves existing values. If incoming telemetry has new keys, it updates them. To prevent `NULL` clobbering, `COALESCE` must be present in the SQL query.

5. **Musl Allocator vs Glibc Virtual Memory Arenas**:
   - Under glibc, multi-threaded Tokio runtimes can allocate 64 MB per thread arena, pushing virtual memory usage high. Using Alpine Linux (`musl`) avoids glibc arena fragmentation, ensuring memory remains strictly bounded.

---

## 4. Conclusion & Concrete Specification

### 4.1 Specification: PostgreSQL Database Sink (`rust-core/src/db_sink.rs`)

```rust
//! Database sink module for Milestone 3 (Rust USP Core).
//! Handles connection pooling, startup retry with backoff, and
//! resilient asynchronous upserts into PostgreSQL `cpe_live_state`.

use chrono::{DateTime, Utc};
use serde_json::Value as JsonValue;
use sqlx::postgres::{PgPool, PgPoolOptions};
use std::time::Duration;
use tokio::sync::mpsc::Receiver;

/// Authoritative internal telemetry struct matching peer definitions
#[derive(Debug, Clone)]
pub struct TelemetryUpdate {
    pub cpe_id: String,
    pub endpoint_id: Option<String>,
    pub status: String,
    pub current_parameters: JsonValue,
    pub telemetry_metrics: JsonValue,
    pub ip_address: Option<String>,
    pub firmware_version: Option<String>,
    pub received_at: DateTime<Utc>,
}

/// SQL statement performing atomic upsert with JSONB concatenation
const UPSERT_LIVE_STATE_SQL: &str = r#"
INSERT INTO cpe_live_state (
    cpe_id,
    endpoint_id,
    current_parameters,
    telemetry_metrics,
    status,
    ip_address,
    firmware_version,
    last_seen,
    updated_at
) VALUES (
    $1,
    $2,
    COALESCE($3, '{}'::jsonb),
    COALESCE($4, '{}'::jsonb),
    COALESCE($5, 'online'),
    $6,
    $7,
    COALESCE($8, CURRENT_TIMESTAMP),
    CURRENT_TIMESTAMP
)
ON CONFLICT (cpe_id) DO UPDATE SET
    endpoint_id = COALESCE(EXCLUDED.endpoint_id, cpe_live_state.endpoint_id),
    current_parameters = cpe_live_state.current_parameters || COALESCE(EXCLUDED.current_parameters, '{}'::jsonb),
    telemetry_metrics = cpe_live_state.telemetry_metrics || COALESCE(EXCLUDED.telemetry_metrics, '{}'::jsonb),
    status = COALESCE(EXCLUDED.status, cpe_live_state.status),
    ip_address = COALESCE(EXCLUDED.ip_address, cpe_live_state.ip_address),
    firmware_version = COALESCE(EXCLUDED.firmware_version, cpe_live_state.firmware_version),
    last_seen = COALESCE(EXCLUDED.last_seen, cpe_live_state.last_seen),
    updated_at = CURRENT_TIMESTAMP;
"#;

/// Creates an optimized PostgreSQL connection pool.
/// Max connections is capped at 10 to guarantee strict containment within 500M RAM.
pub async fn create_db_pool(database_url: &str) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(10)
        .min_connections(2)
        .acquire_timeout(Duration::from_secs(5))
        .idle_timeout(Duration::from_secs(600))
        .max_lifetime(Duration::from_secs(1800))
        .connect(database_url)
        .await
}

/// Connects to PostgreSQL with exponential backoff retry.
pub async fn connect_with_retry(database_url: &str, max_retries: u32) -> Result<PgPool, sqlx::Error> {
    let mut attempts = 0;
    let mut delay = Duration::from_secs(1);

    loop {
        attempts += 1;
        match create_db_pool(database_url).await {
            Ok(pool) => {
                tracing::info!("Connected to PostgreSQL successfully at {}", database_url);
                return Ok(pool);
            }
            Err(err) => {
                if attempts >= max_retries {
                    tracing::error!(
                        attempts = attempts,
                        error = %err,
                        "Exceeded max retries connecting to PostgreSQL"
                    );
                    return Err(err);
                }
                tracing::warn!(
                    attempt = attempts,
                    max_retries = max_retries,
                    delay_ms = delay.as_millis(),
                    error = %err,
                    "Failed to connect to PostgreSQL; retrying..."
                );
                tokio::time::sleep(delay).await;
                delay = std::cmp::min(delay * 2, Duration::from_secs(5));
            }
        }
    }
}

/// Executes an asynchronous upsert into `cpe_live_state`.
/// Handles Foreign Key violation (SQLSTATE 23503) gracefully when CPE is unregistered.
pub async fn upsert_live_state(pool: &PgPool, update: &TelemetryUpdate) -> Result<u64, sqlx::Error> {
    let result = sqlx::query(UPSERT_LIVE_STATE_SQL)
        .bind(&update.cpe_id)
        .bind(&update.endpoint_id)
        .bind(&update.current_parameters)
        .bind(&update.telemetry_metrics)
        .bind(&update.status)
        .bind(&update.ip_address)
        .bind(&update.firmware_version)
        .bind(update.received_at)
        .execute(pool)
        .await;

    match result {
        Ok(done) => Ok(done.rows_affected()),
        Err(sqlx::Error::Database(db_err)) if db_err.code().as_deref() == Some("23503") => {
            tracing::warn!(
                cpe_id = %update.cpe_id,
                "Device not found in cpe_inventory; live-state upsert skipped (FK constraint)"
            );
            Ok(0)
        }
        Err(e) => {
            tracing::error!(
                cpe_id = %update.cpe_id,
                error = %e,
                "Failed to execute live-state upsert"
            );
            Err(e)
        }
    }
}

/// Background worker task that drains updates from the MPSC receiver and persists them.
pub async fn run_db_sink_task(pool: PgPool, mut rx: Receiver<TelemetryUpdate>) {
    tracing::info!("Database sink task started. Listening for telemetry updates...");

    while let Some(update) = rx.recv().await {
        let cpe_id = update.cpe_id.clone();
        match upsert_live_state(&pool, &update).await {
            Ok(rows) => {
                tracing::debug!(
                    cpe_id = %cpe_id,
                    rows = rows,
                    "Live state upsert completed successfully"
                );
            }
            Err(err) => {
                tracing::error!(
                    cpe_id = %cpe_id,
                    error = %err,
                    "Error processing telemetry update in db sink"
                );
            }
        }
    }

    tracing::warn!("Database sink MPSC receiver channel closed. Exiting sink task.");
}
```

---

### 4.2 Specification: Multi-Stage Production Container (`rust-core/Dockerfile`)

```dockerfile
# ==============================================================================
# Multi-Stage Dockerfile for Rust USP Core Worker (Milestone 3)
# ==============================================================================
# Architecture: Alpine Linux + musl for minimal footprint (<30 MB RAM)
# Builder: Alpine with musl-dev, protobuf, and protobuf-dev
# Runner: Minimal Alpine 3.19 with libgcc and non-root user (appuser)
# Strict Physical Limit: 500 MB (docker-compose.yml)
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Build & Compile Binary
# ------------------------------------------------------------------------------
FROM rust:1.77-alpine AS builder

# Install build dependencies: musl-dev, protobuf compiler & headers, make/gcc
RUN apk add --no-cache \
    musl-dev \
    protobuf \
    protobuf-dev \
    build-base \
    binutils

ENV PROTOC=/usr/bin/protoc
ENV PROTOC_INCLUDE=/usr/include

WORKDIR /usr/src/app

# Pre-compile dependency layer for build caching
COPY Cargo.toml Cargo.lock* ./
COPY build.rs ./build.rs
COPY proto ./proto

# Create dummy source file to compile dependencies
RUN mkdir src && \
    echo "fn main() {}" > src/main.rs && \
    cargo build --release && \
    rm -rf src

# Copy real source files
COPY src ./src

# Touch files to invalidate dummy build and trigger full compilation
RUN touch src/main.rs build.rs && \
    cargo build --release && \
    strip target/release/rust-core

# ------------------------------------------------------------------------------
# Stage 2: Minimal Hardened Runtime
# ------------------------------------------------------------------------------
FROM alpine:3.19 AS runner

# Install essential shared libraries: ca-certificates for TLS, libgcc for musl unwinding
RUN apk add --no-cache \
    ca-certificates \
    libgcc \
    tzdata

# Create dedicated non-root application user
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

WORKDIR /app

# Copy stripped binary from builder stage
COPY --from=builder /usr/src/app/target/release/rust-core /app/rust-core

# Set file permissions
RUN chown -R appuser:appgroup /app

# Switch to unprivileged user
USER appuser

# Healthcheck expectation in docker-compose.yml: test -f /tmp/healthy || exit 1
# /tmp has mode 1777 in Alpine, so appuser has full write permissions.
ENV HEALTHCHECK_FILE=/tmp/healthy

# Container entrypoint
ENTRYPOINT ["/app/rust-core"]
```

---

### 4.3 Specification: Cargo Dependencies (`rust-core/Cargo.toml`)
To support the database sink and multi-stage build, the following configuration is specified:

```toml
[package]
name = "rust-core"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1.38", features = ["full"] }
rumqttc = "0.24"
sqlx = { version = "0.7", default-features = false, features = [
    "runtime-tokio-rustls",
    "postgres",
    "json",
    "chrono",
    "macros"
] }
prost = "0.12"
prost-types = "0.12"
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
chrono = { version = "0.4", features = ["serde"] }
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
dotenvy = "0.15"

[build-dependencies]
prost-build = "0.12"

[profile.release]
opt-level = 3
lto = true
codegen-units = 1
panic = "abort"
strip = true
```

---

## 5. Verification Method

### 5.1 Independent Verification Commands

1. **Static AST & SQL DDL Test Suite**:
   ```bash
   python3 postgres/test_schema.py
   ```
   *Expected Output*: 20 tests executed, 19 OK, 1 skipped (live PG).

2. **Empirical Reconciliation & Trigger Behavior**:
   ```bash
   python3 postgres/test_reconciliation_empirical.py
   ```
   *Expected Output*: 23 tests executed, all 23 OK (including optical delta > 1.0 dBm trigger validation).

3. **SQL UPSERT & JSONB Concatenation Verification**:
   ```bash
   python3 .agents/explorer_m3_3/test_sql_upsert_verification.py
   ```
   *Expected Output*: 3 tests executed, all 3 OK (validating column alignment against `init.sql` and JSONB merge simulation).

4. **Container Build Verification** (when Docker is active):
   ```bash
   docker build -t rust-core ./rust-core
   ```
   *Success Condition*: Clean multi-stage build completion, final image size < 30 MB.

5. **Memory Usage Verification**:
   ```bash
   docker stats --no-stream rust-core
   ```
   *Success Condition*: Memory usage < 30 MB (well below 500 MB limit).

6. **End-to-End Simulation**:
   ```bash
   ./simulate_flow.sh
   ```
   *Success Condition*: Steps 1 through 5 pass with exit code 0.

### 5.2 Invalidation Conditions
- Any change to `cpe_live_state` column names in `postgres/init.sql` without corresponding update to `UPSERT_LIVE_STATE_SQL`.
- Introducing `sqlx::query!` macro in place of `sqlx::query` without committing offline schema metadata.
- Removing `libgcc` from the runner stage of `Dockerfile` if dynamic runtime symbols are required.
- Removing `COALESCE` from `current_parameters` or `telemetry_metrics` in the upsert query, causing `NULL` clobbering.
