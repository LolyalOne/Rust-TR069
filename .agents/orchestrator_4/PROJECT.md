# Project: Rust-TR069 Dual-Stack Refactoring (TR-069 Classic + TR-369 USP)

## Architecture
The system operates as a high-performance, asynchronous Dual-Stack Auto Configuration Server (ACS):
1. **TR-369 / USP Track**: Edge devices connect via MQTT broker (Mosquitto on port 1883) exchanging Protobuf/JSON USP Record/Msg payloads. Handled by Tokio MQTT subscriber in `rust-core`.
2. **TR-069 / CWMP Track**: Consumer ONTs (Huawei EchoLife, TP-Link EX) connect via HTTP POST on port 7547 exchanging SOAP/XML (`<cwmp:Inform>`). Handled by embedded Axum HTTP server in `rust-core`.
3. **Unified Ingestion Sink**: Extracted telemetry and parameters from both TR-369 (USP) and TR-069 (CWMP) converge into the **same Tokio MPSC channel** (`tokio::sync::mpsc::Sender<TelemetryUpdate>`). The `run_db_sink` task drains updates into PostgreSQL (`cpe_live_state` in tmpfs `ram_tablespace`).
4. **Reconciliation Trigger**: PostgreSQL trigger `reconcile_live_to_history()` monitors optical Rx power variations, automatically logging historical entries in `cpe_historical_metrics` if delta > 1.0 dBm with zero WAL write amplification.
5. **Command Polling & Delivery**: TR-069 is a client-polled protocol. Commands queued by `python-api` are stored in `cpe_pending_commands` in PostgreSQL. When the ONT finishes its Inform exchange with an empty HTTP POST, `rust-core` fetches pending commands and responds with SOAP RPCs (`cwmp:Reboot`, `cwmp:GetParameterValues`).

## Feature Inventory
Every feature identified during the Survey phase:
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | CWMP Port Exposure | Expose `7547:7547` on `rust-core` in `docker-compose.yml` with `CWMP_PORT` env | M1 | ORIGINAL_REQUEST R5 |
| F2 | Command Queue Schema | Table `cpe_pending_commands` in PostgreSQL with indexes and status lifecycle | M1 | ORIGINAL_REQUEST R4, DB Explorer |
| F3 | FastAPI Command Enqueueing | Python API endpoints to enqueue Reboot & GetParameterValues commands | M1 | ORIGINAL_REQUEST R4, API Explorer |
| F4 | Axum Embedded HTTP Server | Embedded Axum HTTP server running on 0.0.0.0:7547 in Tokio runtime alongside MQTT | M2 | ORIGINAL_REQUEST R1, Rust Explorer |
| F5 | Fast XML Parsing Engine | `roxmltree` zero-allocation parser resilient to vendor namespace prefixes | M2 | ORIGINAL_REQUEST R2, Rust Explorer |
| F6 | Huawei EchoLife Inform Parser | Extract DeviceId (`SerialNumber`, `Manufacturer`) and TR-098 Optical params (`X_HW_OpticalRxPower`) | M2 | ORIGINAL_REQUEST R2, Spec Miner |
| F7 | TP-Link Inform Parser | Extract DeviceId and TR-181 Optical params (`Device.Optical.Interface.1.OpticalSignalLevel`) | M2 | ORIGINAL_REQUEST R2, Spec Miner |
| F8 | MPSC Unified Ingestion | Converge parsed CWMP Inform data into `tokio::sync::mpsc::Sender<TelemetryUpdate>` | M2 | ORIGINAL_REQUEST R3, Rust Explorer |
| F9 | CWMP InformResponse Generation | Return HTTP 200 OK with valid `<cwmp:InformResponse>` SOAP envelope | M2 | Spec Miner |
| F10 | CWMP Session State & Empty POST | Detect empty HTTP POST from ONT shifting session to ACS request phase | M3 | Spec Miner |
| F11 | Pending Command Fetch & Dispatch | Poll `cpe_pending_commands` for ONT serial, generate SOAP RPC (`Reboot`, `GetParameterValues`) | M3 | ORIGINAL_REQUEST R4, Rust Explorer |
| F12 | Command Completion & Empty 200 OK | Process ONT RPC response, update command status in DB, terminate session with empty 200 OK | M3 | Spec Miner |
| F13 | Multi-Container Orchestration | Build and boot `docker compose up -d --build` with all services healthy | M4 | ORIGINAL_REQUEST Acceptance |
| F14 | Huawei EchoLife Curl E2E Test | Post raw Huawei Inform XML to port 7547, verify `cpe_live_state` and trigger | M4 | ORIGINAL_REQUEST Acceptance |
| F15 | TR-369 MQTT Non-Regression | Execute `simulate_flow.sh` verifying 100% pass rate with zero MQTT regression | M4 | ORIGINAL_REQUEST Acceptance |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Infra & Data Layer (Docker & DB Queue) | `docker-compose.yml` port 7547; `postgres/init.sql` `cpe_pending_commands` table; `python-api` command models & endpoints | none | IN_PROGRESS |
| M2 | Rust Core CWMP Server & XML Ingest | `rust-core/Cargo.toml` (`axum`, `roxmltree`); HTTP listener on 7547; Inform parsing; MPSC convergence; `InformResponse` | M1 | PLANNED |
| M3 | CWMP RPC Command Delivery | Empty POST detection; fetch pending commands from PostgreSQL; serialize SOAP RPC; complete command status | M2 | PLANNED |
| M4 | E2E Integration & Verification | Container build; Huawei Inform curl test; DB verification; `simulate_flow.sh` zero-regression verification | M3 | PLANNED |

## Interface Contracts

### `docker-compose.yml` ↔ `rust-core`
- `rust-core` service binds `0.0.0.0:7547` on host port `7547`.
- Environment variable `CWMP_PORT=7547` and `CWMP_HOST=0.0.0.0`.

### `python-api` ↔ PostgreSQL (`cpe_pending_commands`)
- Table `cpe_pending_commands`:
  - `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
  - `cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`
  - `command_type VARCHAR(64) NOT NULL` (e.g. `'Reboot'`, `'GetParameterValues'`)
  - `command_payload JSONB NOT NULL DEFAULT '{}'`
  - `status VARCHAR(32) NOT NULL DEFAULT 'pending'` (`'pending'`, `'dispatched'`, `'completed'`, `'failed'`)
  - `created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP`
  - `dispatched_at TIMESTAMPTZ`
  - `completed_at TIMESTAMPTZ`
  - `result_payload JSONB`
- Indexes: `idx_cpe_pending_commands_lookup` on `(cpe_id, status, created_at)`.

### `rust-core` ↔ PostgreSQL (`cpe_pending_commands`)
- When ONT with `cpe_id` sends empty POST in CWMP session:
  - Query: `SELECT id, command_type, command_payload FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`
  - On dispatch: `UPDATE cpe_pending_commands SET status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP WHERE id = $1`
  - On ONT response: `UPDATE cpe_pending_commands SET status = 'completed', completed_at = CURRENT_TIMESTAMP, result_payload = $2 WHERE id = $1`

### `rust-core` HTTP Server ↔ `run_db_sink` (MPSC Channel)
- Channel: `tokio::sync::mpsc::Sender<TelemetryUpdate>`
- `TelemetryUpdate` fields populated from XML Inform:
  - `cpe_id`: SerialNumber (e.g. `485754431234ABCD`)
  - `endpoint_id`: Option with `cpe_id` or OUI-ProductClass-SerialNumber
  - `status`: `"online"`
  - `current_parameters`: JSON object with all extracted `<ParameterValueStruct>` key-values
  - `telemetry_metrics`: JSON object with `"rx_optical_power"` parsed as float, e.g. `-19.50`
  - `ip_address`: extracted from connection or `ConnectionRequestURL`
  - `firmware_version`: extracted from SoftwareVersion
  - `timestamp`: Utc::now()

## Code Layout
- `docker-compose.yml`: Top-level container orchestration
- `postgres/init.sql`: Hybrid database schema, tablespaces, tables, and triggers
- `python-api/`: FastAPI controller
  - `app/main.py`: Entrypoint & lifecycle
  - `app/models.py`: SQLAlchemy 2.0 ORM models
  - `app/schemas.py`: Pydantic validation schemas
  - `app/routers/cpes.py`: CPE REST endpoints
  - `tests/`: Pytest unit & adversarial suites
- `rust-core/`: High-performance Tokio worker
  - `Cargo.toml`: Rust dependencies
  - `src/main.rs`: Multi-threaded Tokio server, MQTT subscriber, Axum CWMP listener, MPSC worker
  - `src/cwmp/`: Optional/inline CWMP XML parser and SOAP serializer
- `simulate_flow.sh`: End-to-end integration test harness
