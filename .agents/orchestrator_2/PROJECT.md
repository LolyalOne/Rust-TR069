# Project: TR-369 / USP ACS (Auto Configuration Server)

## Architecture
The system is an asynchronous, fault-tolerant, event-driven ACS operating over MQTT, composed of:
1. **Container Infrastructure**: Docker Compose with strict memory limits (Postgres 1.5GB with tmpfs, Mosquitto 500MB, Rust USP Core 500MB, Python FastAPI 1GB).
2. **Mosquitto MQTT Broker**: Central message transfer protocol (MTP) hub for CPEs and controllers on topic hierarchy `usp/endpoint/#`.
3. **PostgreSQL Hybrid Data Model**:
   - `cpe_inventory`: Persistent relational storage for device records.
   - `cpe_live_state`: High-throughput `UNLOGGED` table in RAM (`ram_tablespace` on tmpfs `/var/lib/postgresql/ram_data`).
   - `cpe_historical_metrics`: Persistent time-series audit log populated via PL/pgSQL reconciliation trigger (`reconcile_live_to_history`) strictly on optical signal variation > 1.0 dBm without updating `cpe_inventory`.
4. **Rust USP Core Worker**: High-performance consumer using `tokio`, `rumqttc`, `sqlx`, and `prost` with internal MPSC channel decoupling MQTT ingest from PostgreSQL batch upserting. Decodes TR-369 Protobuf Records and Msg envelopes from `usp/endpoint/#`.
5. **Python FastAPI Manager**: Asynchronous REST API utilizing `SQLAlchemy 2.0`, `asyncpg`, and `aiomqtt` for inventory CRUD, microsecond live-state querying from RAM, and publishing TR-369 commands over MQTT.
6. **Limit Configuration Tool**: Interactive CLI and scripted tool (`configure_limits.py`) to manage and persist container memory limits.
7. **Portability**: Complete `.devcontainer` configuration with VS Code extensions for Rust, Python, and Docker.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Docker Memory Limits | Strict physical memory limits in compose: Postgres 1.5G, Mosquitto 500M, Rust 500M, FastAPI 1G | M1 | R1 |
| 2 | Postgres RAM tmpfs | Mount tmpfs at `/var/lib/postgresql/ram_data` with correct Alpine UID/GID (70:70) | M1 | R1 |
| 3 | Container Healthchecks | Reliable healthcheck definitions for all 4 services ensuring status `healthy` | M1 | Survey |
| 4 | DevContainer Portability | `.devcontainer/devcontainer.json` with VS Code extensions for Rust, Python, Docker | M1 | R2 |
| 5 | Limit Setup CLI Tool | Standalone CLI (`configure_limits.py`) with interactive menu and CLI arguments | M1 | R3 |
| 6 | Database Tablespace | `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data'` without transaction block | M2 | R4, Audit |
| 7 | Persistent CPE Inventory | `cpe_inventory` table storing device metadata, serial, model, created/updated timestamps | M2 | R4 |
| 8 | In-RAM Live State Table | `UNLOGGED TABLE cpe_live_state` in `ram_tablespace` for microsecond volatile writes | M2 | R4 |
| 9 | Persistent State History / Metrics | `cpe_historical_metrics` table storing historical telemetry snapshots | M2 | R4 |
| 10 | Reconciliation Trigger | Trigger `reconcile_live_to_history` triggered on optical signal variation > 1.0 dBm, no UPDATE on cpe_inventory | M2 | R4, Audit |
| 11 | TR-369 Protobuf Schema | Wire-compatible `usp.proto` defining BBF tags for Record, Msg, Operate, Notify | M3 | R5, Spec Miner |
| 12 | Rust MPSC Pipeline | Tokio MPSC channel decoupling MQTT packet ingest from PostgreSQL writes | M3 | R5 |
| 13 | Rust TR-369 Ingest | Subscribe to `usp/endpoint/#`, decode protobuf/json, and upsert into `cpe_live_state` | M3 | R5 |
| 14 | Rust Core Dockerfile | Multi-stage production container build with healthcheck (`rust:alpine` -> `alpine:latest`) | M3 | R1, R5 |
| 15 | FastAPI CRUD Endpoints | REST endpoints for managing `cpe_inventory` (`/api/v1/cpes`) | M4 | R6 |
| 16 | FastAPI Live Status Query | Endpoint returning real-time volatile parameters directly from `cpe_live_state` | M4 | R6 |
| 17 | FastAPI Command Dispatch | Endpoint publishing TR-369 command to MQTT `usp/endpoint/{id}/request` | M4 | R6 |
| 18 | FastAPI Gunicorn & Dockerfile | Python 3.11, Gunicorn with 2 workers, async SQLAlchemy 2.0, Dockerfile | M4 | R1, R6 |
| 19 | E2E Automated Simulation | `simulate_flow.sh` running full 5-step test sequence with exit code 0 | M5 | Acceptance |
| 20 | Complete README.md | Full documentation reflecting all completed milestones and operational instructions | M5 | Acceptance |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Containerized Infra & Setup CLI | docker-compose.yml memory limits, tmpfs, healthchecks, .devcontainer, configure_limits.py | none | DONE |
| M2 | Hybrid PostgreSQL Schema & Triggers | init.sql (fix tablespace outside DO block, fix trigger optical signal > 1.0 dBm without cpe_inventory UPDATE) | M1 | DONE |
| M3 | Rust USP Core Worker | rust-core/ Cargo.toml, build.rs, proto/usp.proto, src/main.rs, Dockerfile | M1, M2 | DONE |
| M4 | Python FastAPI Manager | python-api/ requirements.txt, app/main.py, gunicorn_conf.py, Dockerfile | M1, M2 | DONE |
| M5 | E2E Acceptance & Documentation | docker compose up -d --build, simulate_flow.sh exit code 0, README.md update | M1, M2, M3, M4 | IN_PROGRESS |

## Interface Contracts

### Mosquitto MQTT Broker ↔ Consumers / Publishers
- Inbound Telemetry Topic: `usp/endpoint/{cpe_id}/notify` or wildcard `usp/endpoint/#`
- Outbound Controller Command Topic: `usp/endpoint/{cpe_id}/request`
- Payload: Binary Protobuf TR-369 `Record` carrying serialized `Msg` (with JSON fallback for test simulation).

### Rust USP Core ↔ PostgreSQL Database
- Connection: `DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db`
- Operation: `INSERT INTO cpe_live_state (cpe_id, current_parameters, telemetry_metrics, status, last_seen, updated_at) VALUES (...) ON CONFLICT (cpe_id) DO UPDATE SET ...`

### Python FastAPI ↔ PostgreSQL Database
- Connection: `DATABASE_URL=postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db`
- Tables:
  - `cpe_inventory`: Full CRUD (`SELECT`, `INSERT`, `UPDATE`, `DELETE`)
  - `cpe_live_state`: Fast read (`SELECT current_parameters, telemetry_metrics, status, last_seen FROM cpe_live_state WHERE cpe_id = :id`)
  - `cpe_historical_metrics`: Audit read (`SELECT * FROM cpe_historical_metrics WHERE cpe_id = :id ORDER BY recorded_at DESC`)

### Python FastAPI ↔ Mosquitto MQTT
- Connection: Host `mosquitto`, Port `1883`, Client ID `fastapi-manager`
- Method: `publish_command(cpe_id, command, parameters)` publishing to `usp/endpoint/{cpe_id}/request` with QoS 1.

## Code Layout
```
/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/
├── docker-compose.yml              # Container infrastructure
├── configure_limits.py             # Memory limit setup tool
├── simulate_flow.sh                # Automated simulation script
├── README.md                       # Project documentation
├── .devcontainer/
│   └── devcontainer.json
├── mosquitto/
│   └── mosquitto.conf              # MQTT Broker configuration
├── postgres/
│   ├── init.sql                    # Hybrid database schema & triggers
│   ├── test_schema.py              # Schema DDL unit test suite
│   └── test_reconciliation_empirical.py # Empirical reconciliation trigger verification
├── rust-core/                      # Rust USP Core Worker
│   ├── Cargo.toml
│   ├── build.rs
│   ├── Dockerfile
│   ├── proto/
│   │   └── usp.proto               # BBF-compatible TR-369 protobuf definitions
│   └── src/
│       └── main.rs
└── python-api/                     # FastAPI Manager
    ├── Dockerfile
    ├── requirements.txt
    ├── gunicorn_conf.py
    └── app/
        └── main.py
```
