# Project: TR-369 / USP ACS (Auto Configuration Server)

## Architecture
The system is an asynchronous, fault-tolerant, event-driven ACS operating over MQTT, composed of:
1. **Container Infrastructure**: Docker Compose with strict memory limits (Postgres 1.5GB with tmpfs, Mosquitto 500MB, Rust USP Core 500MB, Python FastAPI 1GB).
2. **Mosquitto MQTT Broker**: Central message transfer protocol (MTP) hub for CPEs and controllers on topic hierarchy `usp/endpoint/#`.
3. **PostgreSQL Hybrid Data Model**:
   - `cpe_inventory`: Persistent relational storage for device records.
   - `cpe_live_state`: High-throughput `UNLOGGED` table in RAM (`ram_tablespace` on tmpfs `/var/lib/postgresql/ram_data`).
   - `cpe_state_history`: Persistent time-series audit log populated via PL/pgSQL reconciliation trigger (`fn_reconcile_cpe_live_state`).
4. **Rust USP Core Worker**: High-performance consumer using `tokio`, `rumqttc`, `sqlx`, and `prost` with internal MPSC channel decoupling MQTT ingest from PostgreSQL batch upserting. Decodes TR-369 Protobuf Records and Msg envelopes.
5. **Python FastAPI Manager**: Asynchronous REST API utilizing `SQLAlchemy 2.0`, `asyncpg`, and `aiomqtt` for inventory CRUD, microsecond live-state querying from RAM, and publishing TR-369 Reboot commands over MQTT.
6. **Limit Configuration Tool**: Interactive CLI and scripted tool (`configure_limits.py`) to manage and persist container memory limits.
7. **Portability**: Complete `.devcontainer` configuration with VS Code extensions for Rust, Python, and Docker.

```
+-----------------------------------------------------------------------------------+
|                                 Host / Network                                    |
|                                                                                   |
|  +--------------------+         +-------------------+       +------------------+  |
|  |   CPE / Simulator  |         | Operator / Admin  |       | configure_limits |  |
|  +---------+----------+         +---------+---------+       +--------+---------+  |
|            |                              |                          |            |
|       MQTT 1883                      HTTP 8000                       | Modifies   |
+------------|------------------------------|--------------------------|------------+
|            v                              v                          v            |
|  +--------------------+         +-------------------+      +-------------------+  |
|  |  Eclipse Mosquitto |         |   Python FastAPI  |      | docker-compose.yml|  |
|  |    Broker (500M)   |<------->|   Manager (1G)    |      |  (Memory Limits)  |  |
|  +---------+----------+  MQTT   +---------+---------+      +-------------------+  |
|            |                              |                                       |
|       MQTT Ingest                    SQLAlchemy 2.0                               |
|   usp/endpoint/#                          |                                       |
|            v                              |                                       |
|  +--------------------+                   |                                       |
|  | Rust USP Core (500M|                   |                                       |
|  |   Tokio MPSC /     |                   |                                       |
|  |   Prost Protobuf   |                   |                                       |
|  +---------+----------+                   |                                       |
|            | SQLx Upsert                  |                                       |
|            v                              v                                       |
|  +--------------------------------------------------+                             |
|  |             PostgreSQL Database (1.5G)           |                             |
|  |  +--------------------+   +-------------------+  |                             |
|  |  |   cpe_inventory    |   |  cpe_live_state   |  |                             |
|  |  |    (Persistent)    |   |  (UNLOGGED / RAM) |  |                             |
|  |  +--------------------+   +---------+---------+  |                             |
|  |            ^                        |            |                             |
|  |            |               Reconcile Trigger     |                             |
|  |            |                        v            |                             |
|  |            +--------------+-------------------+  |                             |
|  |                           | cpe_state_history |  |                             |
|  |                           |   (Persistent)    |  |                             |
|  |                           +-------------------+  |                             |
|  +--------------------------------------------------+                             |
+-----------------------------------------------------------------------------------+
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Docker Memory Limits | Strict physical memory limits in compose: Postgres 1.5G, Mosquitto 500M, Rust 500M, FastAPI 1G | M1 | R1 |
| 2 | Postgres RAM tmpfs | Mount tmpfs at `/var/lib/postgresql/ram_data` with correct Alpine UID/GID (70:70) | M1 | R1, Survey |
| 3 | Container Healthchecks | Reliable healthcheck definitions for all 4 services ensuring status `healthy` | M1 | Survey, Acceptance #3 |
| 4 | DevContainer Portability | `.devcontainer/devcontainer.json` with VS Code extensions for Rust, Python, Docker | M1 | R2 |
| 5 | Limit Setup CLI Tool | Standalone CLI (`configure_limits.py`) with interactive menu and CLI arguments | M1 | R3 |
| 6 | Database Tablespace | `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data'` | M2 | R4 |
| 7 | Persistent CPE Inventory | `cpe_inventory` table storing device metadata, serial, model, created/updated timestamps | M2 | R4 |
| 8 | In-RAM Live State Table | `UNLOGGED TABLE cpe_live_state` in `ram_tablespace` for microsecond volatile writes | M2 | R4 |
| 9 | Persistent State History | `cpe_state_history` table storing historical telemetry snapshots | M2 | R4 |
| 10 | Reconciliation Trigger | PL/pgSQL trigger automatically snapshotting validated transitions to history | M2 | R4 |
| 11 | TR-369 Protobuf Schema | Wire-compatible `usp.proto` defining BBF tags for Record, Msg, Operate, Notify | M3 | R5, Spec Miner |
| 12 | Rust MPSC Pipeline | Tokio MPSC channel decoupling MQTT packet ingest from PostgreSQL writes | M3 | R5 |
| 13 | Rust TR-369 Ingest | Subscribe to `usp/endpoint/#`, decode protobuf/json, and upsert into `cpe_live_state` | M3 | R5 |
| 14 | Rust Core Dockerfile | Multi-stage production container build with healthcheck | M3 | R1, R5 |
| 15 | FastAPI CRUD Endpoints | REST endpoints for managing `cpe_inventory` (`/api/v1/cpes`) | M4 | R6 |
| 16 | FastAPI Live Status Query | Endpoint returning real-time volatile parameters directly from `cpe_live_state` | M4 | R6 |
| 17 | FastAPI Command Dispatch | Endpoint publishing TR-369 `Operate` (Reboot) command to MQTT `usp/endpoint/{id}/request` | M4 | R6 |
| 18 | FastAPI Dockerfile | Python 3.12 container build with healthcheck | M4 | R1, R6 |
| 19 | E2E Automated Simulation | `simulate_flow.sh` running full 5-step test sequence with exit code 0 | M5 | Acceptance #4-5 |
| 20 | Git Repo Initialization | Git init, `.gitignore` tracking `Cargo.lock`, remote `https://github.com/LolyalOne/Rust-TR069.git` | M6 | R7 |
| 21 | Git Push to Remote | Commit complete codebase and push to GitHub remote | M6 | R7, Acceptance #6 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Containerized Infra & Setup CLI | docker-compose.yml memory limits, tmpfs fix (uid 70), healthchecks, .devcontainer, configure_limits.py | none | DONE |
| M2 | Hybrid PostgreSQL Schema & Triggers | init.sql, ram_tablespace, cpe_inventory, cpe_live_state (unlogged), cpe_state_history, reconciliation trigger | M1 | IN_PROGRESS |
| M3 | Rust USP Core Worker | proto/usp.proto, Cargo.toml, tokio/rumqttc/sqlx/prost, MPSC pipeline, Dockerfile | M1, M2 | PLANNED |
| M4 | Python FastAPI Manager | FastAPI app, SQLAlchemy 2.0, aiomqtt, CRUD, live-state query, MQTT command dispatch, Dockerfile | M1, M2 | PLANNED |
| M5 | E2E Testing Suite & Simulation | Comprehensive test runner, simulate_flow.sh, 5-step automated validation, TEST_READY.md | M1, M2, M3, M4 | PLANNED |
| M6 | Git Version Control & Remote Push | Git init, clean .gitignore, initial commit, remote setup, push to https://github.com/LolyalOne/Rust-TR069.git | M5 | PLANNED |

## Interface Contracts

### Mosquitto MQTT Broker ↔ Consumers / Publishers
- Inbound Telemetry Topic: `usp/endpoint/{cpe_id}/notify` or `usp/endpoint/{cpe_id}/telemetry` (wildcard: `usp/endpoint/#`)
- Outbound Controller Command Topic: `usp/endpoint/{cpe_id}/request` or `usp/endpoint/{cpe_id}/command`
- Payload: Binary Protobuf TR-369 `Record` carrying serialized `Msg` (with JSON fallback for test simulation).

### Rust USP Core ↔ PostgreSQL Database
- Connection: `DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db`
- Operation: `INSERT INTO cpe_live_state (cpe_id, current_parameters, telemetry_metrics, status, last_seen, updated_at) VALUES (...) ON CONFLICT (cpe_id) DO UPDATE SET ...`

### Python FastAPI ↔ PostgreSQL Database
- Connection: `DATABASE_URL=postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db`
- Tables:
  - `cpe_inventory`: Full CRUD (`SELECT`, `INSERT`, `UPDATE`, `DELETE`)
  - `cpe_live_state`: Fast read (`SELECT current_parameters, telemetry_metrics, status, last_seen FROM cpe_live_state WHERE cpe_id = :id`)
  - `cpe_state_history`: Audit read (`SELECT * FROM cpe_state_history WHERE cpe_id = :id ORDER BY recorded_at DESC`)

### Python FastAPI ↔ Mosquitto MQTT
- Connection: Host `mosquitto`, Port `1883`, Client ID `fastapi-manager`
- Method: `publish_command(cpe_id, command, parameters)` publishing to `usp/endpoint/{cpe_id}/request` with QoS 1.

## Code Layout
```
/mnt/d/Projetos/TR069-181/
├── docker-compose.yml              # R1 Container infrastructure
├── configure_limits.py             # R3 Interactive & CLI memory limit setup tool
├── simulate_flow.sh                # Acceptance 5-step automated simulation script
├── .gitignore                      # Git configuration tracking Cargo.lock
├── .devcontainer/                  # R2 Portability
│   └── devcontainer.json
├── mosquitto/
│   └── mosquitto.conf              # MQTT Broker configuration
├── postgres/
│   └── init.sql                    # R4 Hybrid database schema & triggers
├── rust-core/                      # R5 Rust USP Core Worker
│   ├── Cargo.toml
│   ├── build.rs
│   ├── Dockerfile
│   ├── proto/
│   │   └── usp.proto               # BBF-compatible TR-369 protobuf definitions
│   └── src/
│       ├── main.rs
│       ├── config.rs
│       ├── mqtt.rs
│       ├── db.rs
│       └── models.rs
└── python-api/                     # R6 FastAPI Manager
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── __init__.py
        ├── main.py
        ├── config.py
        ├── database.py
        ├── models.py
        ├── schemas.py
        ├── mqtt_client.py
        └── api/
            ├── __init__.py
            └── v1/
                ├── __init__.py
                ├── router.py
                ├── cpes.py
                └── commands.py
```
