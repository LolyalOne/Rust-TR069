# TR-369 / USP ACS — Test Readiness & E2E Verification Specification

## 1. Overview & Test Architecture

This test specification defines the test suites, verification tiers, execution commands, and feature coverage matrices for the TR-369/USP Auto Configuration Server (ACS) operating over MQTT.

The system is validated through an opaque-box, event-driven verification harness that exercises external entry points:
- **Docker Compose & Physical Resource Limits** (`docker-compose.yml`, `configure_limits.py`)
- **FastAPI Manager REST API** (`http://localhost:8000`)
- **Eclipse Mosquitto MQTT Broker** (`localhost:1883`, topic space `usp/endpoint/#`)
- **PostgreSQL Hybrid Model** (Persistent `cpe_inventory`, in-RAM `cpe_live_state` on `tmpfs`, and persistent `cpe_state_history` populated via PL/pgSQL reconciliation trigger)
- **Rust USP Core Worker** (Tokio MPSC pipeline, Prost TR-369 Protobuf / JSON deserialization, SQLx upsert)
- **Automated CLI Simulation Script** (`simulate_flow.sh`)

---

## 2. Test Runner Commands

### 2.1 Acceptance End-to-End Simulation Runner (Primary)
```bash
# Execute the full 5-step automated CLI simulation sequence
./simulate_flow.sh

# Verbose execution with custom target options
./simulate_flow.sh --verbose --api-url http://localhost:8000 --mqtt-host localhost --mqtt-port 1883 --timeout 45
```
**Exit Code Semantics**:
- `0`: All 5 simulation steps passed with 100% assertion satisfaction.
- Non-zero (`1`): Specific assertion failure with diagnostic logs identifying the failing component.

### 2.2 Component & Tier-Specific Test Runners

| Tier / Target | Command | Verification Scope |
|---|---|---|
| **Full E2E Simulation** | `./simulate_flow.sh` | 5-step automated device lifecycle, telemetry ingest, RAM table update, reconciliation trigger, and command round-trip |
| **Memory Limits CLI (R3)** | `./configure_limits.py --verify`<br>`./configure_limits.py --show` | Docker Compose physical memory limit validation and interactive/CLI updater |
| **Container Infrastructure (R1)** | `docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Health}}"` | All 4 services running and reporting `healthy` status |
| **PostgreSQL Hybrid Schema (R4)** | `docker compose exec postgres psql -U acs_user -d acs_db -c "\dt+ cpe_*"` | Confirms `cpe_live_state` is `UNLOGGED` in `ram_tablespace`, and `cpe_inventory` & `cpe_state_history` are persistent |
| **FastAPI Unit / Integration (R6)** | `pytest python-api/tests/ -v` | CRUD endpoints, Pydantic schemas, async SQLAlchemy sessions, and aiomqtt dispatching |
| **Rust Core Unit / Integration (R5)** | `cargo test --manifest-path rust-core/Cargo.toml` | Protobuf codec, MPSC channel message passing, and SQLx batch upsert logic |
| **Git Remote & VCS (R7)** | `git remote -v`<br>`git status` | Verifies remote points to `https://github.com/LolyalOne/Rust-TR069.git` with clean tree |

---

## 3. The 5-Step Simulation Flow (`simulate_flow.sh`)

The automated simulation script strictly executes the five verification stages required by `ORIGINAL_REQUEST.md`:

```
+--------------------------------------------------------------------------------------------------+
|                                    simulate_flow.sh Execution Flow                               |
|                                                                                                  |
| [Step 0] Pre-Flight Checks: Polling FastAPI /health and Mosquitto probe; clearing prior test state|
|                                                                                                  |
| [Step 1] Device Registration:                                                                    |
|          POST /api/v1/cpes -> HTTP 201 Created -> Assert cpe_id in persistent cpe_inventory     |
|                                                                                                  |
| [Step 2] Telemetry Ingest:                                                                       |
|          Publish TR-369 payload -> MQTT topic usp/endpoint/{cpe_id}/telemetry (QoS 1)            |
|                                                                                                  |
| [Step 3] Rust Worker & RAM State Verification:                                                   |
|          Poll GET /api/v1/cpes/{cpe_id}/live-state -> Assert status=online, cpu_usage=42.5       |
|          (Verifies MPSC pipeline consumed MQTT message and wrote to UNLOGGED ram_tablespace)     |
|                                                                                                  |
| [Step 4] Metric Alteration & Trigger Reconciliation:                                             |
|          Publish altered metric (cpu_usage=88.4) -> Poll /api/v1/cpes/{cpe_id}/history           |
|          Assert >= 2 history snapshots created by PL/pgSQL fn_reconcile_cpe_live_state trigger   |
|                                                                                                  |
| [Step 5] Remote Command Dispatch Round-Trip:                                                     |
|          Background subscriber listens on usp/endpoint/{cpe_id}/#                                |
|          POST /api/v1/cpes/{cpe_id}/reboot -> HTTP 202 -> Assert broker captured Reboot payload  |
|                                                                                                  |
| [Result] Banner logged, temporary files cleaned up, exits with code 0                            |
+--------------------------------------------------------------------------------------------------+
```

---

## 4. Test Tiers Specification

### Tier 1: Functional Verification (Happy Path)
- **T1.1 Container Orchestration**: All 4 services (`postgres`, `mosquitto`, `rust-core`, `python-api`) boot under physical memory caps and achieve `healthy` status.
- **T1.2 DevContainer Initialization**: `.devcontainer/devcontainer.json` mounts workspace and installs Rust, Python, and Docker extensions.
- **T1.3 Memory Limits CLI**: `configure_limits.py` reads current limits, applies valid updates, and preserves YAML structure.
- **T1.4 Database Tablespace & Schema**: PostgreSQL creates `ram_tablespace` on `/var/lib/postgresql/ram_data` (`tmpfs`) and executes `init.sql`.
- **T1.5 CPE Registration**: `POST /api/v1/cpes` persists device record with serial number, model, and manufacturer.
- **T1.6 In-RAM State Upsert**: Rust Core consumes MQTT telemetry and populates `cpe_live_state`.
- **T1.7 Live Query**: `GET /api/v1/cpes/{cpe_id}/live-state` returns sub-millisecond in-memory JSON state.
- **T1.8 State Reconciliation**: Metric change triggers insertion into `cpe_state_history`.
- **T1.9 Reboot Dispatch**: `POST /api/v1/cpes/{cpe_id}/reboot` produces MQTT command on `usp/endpoint/{cpe_id}/request`.
- **T1.10 Git Configuration**: Local git repository configured with remote `https://github.com/LolyalOne/Rust-TR069.git`.

### Tier 2: Boundary & Negative Cases
- **T2.1 Invalid Memory Limits**: `configure_limits.py --limit 99999Z` or `--limit -500M` rejected with clear error message.
- **T2.2 Duplicate CPE Registration**: Registering existing `cpe_id` or duplicate `serial_number` returns HTTP 400 or 409 Conflict.
- **T2.3 Nonexistent CPE Query**: `GET /api/v1/cpes/cpe-unknown/live-state` returns HTTP 404 Not Found.
- **T2.4 Malformed MQTT Payloads**: Non-JSON / corrupted Protobuf bytes sent to `usp/endpoint/{cpe_id}/telemetry` handled gracefully by Rust worker without crashing the MPSC loop.
- **T2.5 Database Connection Resilience**: Transient database restart causes Rust worker and FastAPI connection pool to retry with exponential backoff rather than terminating.
- **T2.6 MQTT Reconnect Behavior**: Mosquitto restart handled by `rumqttc` and `aiomqtt` auto-reconnect loops.
- **T2.7 Unregistered Device Telemetry**: Telemetry arriving for unknown `cpe_id` either rejected or automatically handled per configuration.
- **T2.8 Foreign Key Cascade**: `DELETE /api/v1/cpes/{cpe_id}` cascades deletion to `cpe_live_state` and `cpe_state_history`.

### Tier 3: Cross-Feature & Concurrency Stress
- **T3.1 High-Throughput Burst Ingest**: 1,000 telemetry messages sent in rapid succession; MPSC channel queues updates while PostgreSQL writer applies atomic JSONB merges.
- **T3.2 RAM Table Volatility & Recovery**: Postgres container restarted; volatile `cpe_live_state` clears, but `cpe_inventory` and `cpe_state_history` persist on durable storage without data loss.
- **T3.3 Concurrent API Operations**: Simultaneous REST reads and writes executed without transaction deadlocks.
- **T3.4 Memory Cap Enforcement**: Stress testing services does not breach the configured Docker physical memory limits (`1.5G`, `500M`, `500M`, `1G`).

### Tier 4: Real-World Scenarios
- **Scenario 1: Complete Device Provisioning & Telemetry Streaming**: Cold-start CPE registered, streams periodic telemetry, state verified in RAM and historical log.
- **Scenario 2: Critical Telemetry Metric Spike & Alert Reconciliation**: Parameter alteration (e.g. CPU > 85%, temperature > 50°C) verified to persist change audit snapshot with reason `telemetry_metrics_changed`.
- **Scenario 3: Remote Device Reboot Cycle**: Operator dispatches reboot; message captured over MQTT, CPE acknowledges, live status transitions from `online` to `rebooting` to `online`.
- **Scenario 4: Cold-Start Zero-to-Healthy Deployment**: `docker compose down -v && docker compose up -d` brings full cluster up from scratch with all migrations cleanly applied.

---

## 5. Feature Coverage Matrix

| Feature # | Feature Description | Requirement | Test Tier | Verifying Test / Script | Status |
|---|---|---|---|---|---|
| F1 | Docker Physical Memory Limits | R1 | Tier 1, 3 | `docker stats`, `configure_limits.py --verify` | READY |
| F2 | PostgreSQL tmpfs Mount (UID 70) | R1 | Tier 1 | `docker compose exec postgres mount \| grep ram_data` | READY |
| F3 | Healthchecks for All 4 Services | Acceptance #3 | Tier 1 | `docker compose ps` | READY |
| F4 | DevContainer Portability Config | R2 | Tier 1 | `.devcontainer/devcontainer.json` syntax & extensions | READY |
| F5 | Memory Limit Interactive/CLI Setup | R3 | Tier 1, 2 | `./configure_limits.py --show`, `--service`, `--preset` | READY |
| F6 | PostgreSQL `ram_tablespace` | R4 | Tier 1 | `SELECT spcname FROM pg_tablespace;` | READY |
| F7 | Persistent `cpe_inventory` Table | R4 | Tier 1, 2 | `simulate_flow.sh` (Step 1), `pytest` | READY |
| F8 | UNLOGGED RAM Table `cpe_live_state` | R4 | Tier 1, 3 | `simulate_flow.sh` (Step 3), `psql \dt+` | READY |
| F9 | Historical Table `cpe_state_history` | R4 | Tier 1 | `simulate_flow.sh` (Step 4), `psql \dt+` | READY |
| F10 | PL/pgSQL Reconciliation Trigger | R4 | Tier 1, 4 | `simulate_flow.sh` (Step 4) | READY |
| F11 | TR-369 Protobuf Wire Schema | R5 | Tier 1, 2 | `rust-core/proto/usp.proto`, `cargo test` | READY |
| F12 | Tokio MPSC Channel Decoupling | R5 | Tier 1, 3 | `rust-core/src/main.rs`, `simulate_flow.sh` | READY |
| F13 | Rust Worker Ingest & Upsert | R5 | Tier 1, 2 | `simulate_flow.sh` (Step 2 & 3) | READY |
| F14 | Rust Core Docker Packaging | R1, R5 | Tier 1 | `docker compose ps rust-core` | READY |
| F15 | FastAPI CRUD `/api/v1/cpes` | R6 | Tier 1, 2 | `simulate_flow.sh` (Step 1), `pytest` | READY |
| F16 | FastAPI Live Status `/live-state` | R6 | Tier 1 | `simulate_flow.sh` (Step 3), `pytest` | READY |
| F17 | FastAPI Command Dispatch `/reboot` | R6 | Tier 1, 4 | `simulate_flow.sh` (Step 5), `pytest` | READY |
| F18 | FastAPI Docker Packaging | R1, R6 | Tier 1 | `docker compose ps python-api` | READY |
| F19 | Automated 5-Step E2E Simulation | Acceptance #4-5 | Tier 1, 4 | `./simulate_flow.sh` (Guaranteed Code 0) | READY |
| F20 | Git Repository Initialization | R7 | Tier 1 | `git status`, `.gitignore` | READY |
| F21 | Git Push to GitHub Remote | R7, Acceptance #6 | Tier 1 | `git remote -v`, `git push origin main` | READY |

---

## 6. Pre-Requisites & Execution Runbook

1. **Verify Environment**:
   ```bash
   bash -n simulate_flow.sh
   python3 -c "import paho.mqtt; print('MQTT client ready')"
   ```

2. **Boot Infrastructure**:
   ```bash
   docker compose up -d --build
   ```

3. **Wait for Health Status**:
   ```bash
   docker compose ps
   # Ensure postgres, mosquitto, rust-core, and python-api all report (healthy)
   ```

4. **Execute Full Automated Verification**:
   ```bash
   ./simulate_flow.sh
   echo "Exit Code: $?" # Must be 0
   ```
