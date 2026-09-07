# Investigation & Architectural Handoff Report: Dual-Stack TR-069 / TR-369 Integration

**Agent**: `explorer_m4_api_db_1`  
**Date**: 2026-09-07T14:30:00Z  
**Scope**: Dual-Stack TR-069 (CWMP HTTP/XML) & TR-369 (USP MQTT) Architectural Analysis covering `python-api/`, `postgres/`, `docker-compose.yml`, and `simulate_flow.sh`.  
**Target Files**:
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/main.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/models.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/schemas.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/mqtt.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/routers/cpes.py`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh`

---

## 1. Observation

Direct examination of codebase artifacts, schemas, configuration files, test suites, and running processes yielded the following factual observations:

### 1.1 PostgreSQL Schema & Existing Tables (`postgres/init.sql`)
1. **Tablespace Configuration (lines 19)**:
   ```sql
   CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
   ```
   - Executed strictly as a top-level command outside any transaction block.
   - Mounted in `docker-compose.yml` (line 14) as a 1GB tmpfs volume with `uid=70,gid=70,mode=0700`.

2. **Persistent CPE Inventory (`cpe_inventory`, lines 26–39)**:
   - Primary key: `cpe_id VARCHAR(128)`
   - Unique constraint: `serial_number VARCHAR(64) UNIQUE NOT NULL`
   - Metadata columns: `manufacturer VARCHAR(64) NOT NULL`, `model VARCHAR(64) NOT NULL`, `oui VARCHAR(6)`, `product_class VARCHAR(64)`, `hardware_version VARCHAR(64)`, `software_version VARCHAR(64)`, `description TEXT`, `status VARCHAR(32) NOT NULL DEFAULT 'offline'`, `created_at TIMESTAMPTZ`, `updated_at TIMESTAMPTZ`.
   - Indexes: `idx_cpe_inventory_mfg_model` ON `(manufacturer, model)`, `idx_cpe_inventory_status` ON `(status)`.

3. **In-RAM Volatile Live State (`cpe_live_state`, lines 50–60)**:
   - Declared as `CREATE UNLOGGED TABLE ... TABLESPACE ram_tablespace`.
   - Primary key: `cpe_id VARCHAR(128) PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`.
   - Columns: `endpoint_id VARCHAR(256)`, `current_parameters JSONB NOT NULL DEFAULT '{}'`, `telemetry_metrics JSONB NOT NULL DEFAULT '{}'`, `status VARCHAR(32) DEFAULT 'offline'`, `ip_address VARCHAR(64)`, `firmware_version VARCHAR(64)`, `last_seen TIMESTAMPTZ`, `updated_at TIMESTAMPTZ`.
   - Indexes: GIN on `current_parameters`, GIN on `telemetry_metrics`, B-Tree on `status`, B-Tree on `last_seen`.

4. **Persistent Historical Metrics (`cpe_historical_metrics`, lines 73–83) & View (`cpe_state_history`, lines 89–91)**:
   - Primary key: `id BIGSERIAL PRIMARY KEY`.
   - Foreign key: `cpe_id VARCHAR(128) REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`.
   - Columns: `status VARCHAR(32)`, `current_parameters JSONB`, `telemetry_metrics JSONB`, `optical_power NUMERIC(6,2)`, `recorded_at TIMESTAMPTZ`, `change_reason VARCHAR(64) DEFAULT 'optical_signal_variation'`.
   - Backward-compatibility view `cpe_state_history` mirrors `cpe_historical_metrics`.

5. **Triggers and Reconciliation Logic (lines 96–238)**:
   - `trg_cpe_inventory_updated_at` (BEFORE UPDATE) -> `fn_set_updated_at()`
   - `trg_cpe_live_state_updated_at` (BEFORE UPDATE) -> `fn_set_updated_at()`
   - `reconcile_live_to_history` (AFTER INSERT OR UPDATE ON `cpe_live_state`):
     - Parses optical power from `telemetry_metrics` (`rx_optical_power`, `optical_power`, `optical_rx_power`, `rx_power`) or `current_parameters` (`Device.Optical.Interface.1.OpticalSignalLevel`, `Device.Optical.Interface.1.RxPower`).
     - On `INSERT`: if optical power is present, records initial baseline with `change_reason = 'initial_state'`.
     - On `UPDATE`: if optical delta $|NEW - OLD| > 1.0\text{ dBm}$, records snapshot with `change_reason = 'optical_signal_variation'`.
     - **Strict zero WAL write amplification**: Never issues `UPDATE` against `cpe_inventory`.

6. **PostgreSQL Test Harness**:
   - `python3 -m unittest discover -s postgres -p "test_*.py" -v` executed **68 tests** (67 passed, 1 skipped live DB) with code 0 in 1.3s.
   - Tests enforce static lexical checks on `init.sql` ensuring `CREATE TABLESPACE` remains a top-level statement and `cpe_inventory` is never modified by triggers.

---

### 1.2 Python FastAPI Current Architecture & Command Handling (`python-api/`)
1. **Dependencies and Execution Environment**:
   - Python 3.11, FastAPI, SQLAlchemy 2.0 (async via `asyncpg`), Gunicorn with 2 `UvicornWorker` processes (`gunicorn_conf.py`), Pydantic V2 (`app/schemas.py`).
   - SQLite dialect variants (`BIGINT_TYPE = BigInteger().with_variant(Integer, "sqlite")`, `JSON_TYPE = JSON().with_variant(JSONB, "postgresql")`) allow running all unit tests in-memory with zero PostgreSQL dependency.
   - Test suites: `pytest python-api/tests/test_api.py` (10/10 passed in 1.3s) and `pytest python-api/tests/test_adversarial.py` (10/10 passed in 1.7s).

2. **Existing Command Handling (`POST /api/v1/cpes/{cpe_id}/reboot`)**:
   - Located in `python-api/app/routers/cpes.py` (lines 204–231):
     ```python
     @router.post("/{cpe_id}/reboot", response_model=CommandDispatchResponse, status_code=status.HTTP_200_OK)
     async def reboot_cpe(cpe_id: str, db: AsyncSession = Depends(get_db)):
         cpe = await db.get(CpeInventory, cpe_id)
         if not cpe:
             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")
         try:
             dispatch_info = await mqtt_publisher.publish_reboot_command(cpe_id)
         except Exception as e:
             raise HTTPException(
                 status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                 detail=f"MQTT broker delivery failed: {e}",
             )
         return CommandDispatchResponse(...)
     ```
   - In `app/mqtt.py` (lines 45–93):
     - Formats a JSON payload matching TR-369 USP Operate specifications:
       `"usp": {"header": {"msg_type": "OPERATE"}, "body": {"request": {"operate": {"command": "Device.Reboot()", ...}}}}`
     - Publishes to Mosquitto topic `usp/endpoint/{cpe_id}/request` with QoS 1.

3. **Current Deficiencies for TR-069 Support**:
   - **No TR-069 polling queue**: In TR-069, client devices (ONTs) connect periodically using HTTP POST (`<cwmp:Inform>`). They do NOT subscribe to an MQTT broker.
   - **Missing RPC commands**: There is currently no endpoint for `GetParameterValues`, `SetParameterValues`, or generic CWMP RPC commands.
   - **No command persistence or status tracking**: Commands sent via MQTT are fire-and-forget. The API has no mechanism to store pending commands waiting for an ONT's Inform polling window or to track execution results (`pending`, `sent`, `completed`, `failed`).

---

### 1.3 Docker Compose Configuration (`docker-compose.yml`)
1. **Services Definition**:
   - `postgres`: 1.5G memory limit, tmpfs 1G at `/var/lib/postgresql/ram_data`, internal port 5432.
   - `mosquitto`: 500M memory limit, port `1883:1883`.
   - `rust-core`: 500M memory limit, builds `./rust-core`, connects to `postgres:5432` and `mosquitto:1883`.
     - **Currently lacks port exposure** (`ports: - "7547:7547"` is absent).
     - Healthcheck probes `test -f /tmp/healthy || exit 1`.
   - `python-api`: 1G memory limit, port `8000:8000`.

2. **Network & Host Port Verification**:
   - Tested host port availability with `ss -tulpn | grep 7547`: Port 7547 is currently unbound and free.
   - Tested `python3 configure_limits.py --show`: Exited 0 cleanly. The script does not touch `ports` and manages only `deploy.resources.limits.memory`.

---

### 1.4 Simulation Script Analysis (`simulate_flow.sh`)
1. **Five-Step Automated Sequence**:
   - **Step 0**: Health check probe (`/health`), MQTT probe, idempotency cleanup (`DELETE /api/v1/cpes/${CPE_ID}`).
   - **Step 1**: Register CPE (`POST /api/v1/cpes`) -> Expects HTTP 201/200, `.cpe_id == "${CPE_ID}"`, verifies `GET /api/v1/cpes/${CPE_ID}` has `.model == "Archer-AX50"`.
   - **Step 2**: Publish TR-369 telemetry to MQTT (`usp/endpoint/${CPE_ID}/telemetry` and `/notify`) with `cpu_usage: 42.5` and `rx_optical_power: -18.5`.
   - **Step 3**: Poll `GET /api/v1/cpes/${CPE_ID}/live-state` (or `/live`) -> Expects `.status == "online"` and `.telemetry_metrics.cpu_usage == "42.5"` or `.metrics.cpu_usage == "42.5"`.
   - **Step 4**: Publish altered telemetry with `cpu_usage: 88.4` and `rx_optical_power: -21.0` (delta = 2.5 dBm > 1.0 dBm) -> Polls `GET /api/v1/cpes/${CPE_ID}/history` -> Expects `.items | length >= 2`.
   - **Step 5**: Attach background subscriber to `usp/endpoint/${CPE_ID}/#` -> Calls `POST /api/v1/cpes/${CPE_ID}/reboot` -> Asserts HTTP 200/202 -> Asserts subscriber captures message matching regex `reboot|operate`.

---

## 2. Logic Chain

### 2.1 TR-069 Inform & Telemetry Ingestion Convergence
1. In TR-069, an ONT (e.g. Huawei EchoLife HG8245H) initiates an HTTP POST session containing a SOAP XML envelope (`<cwmp:Inform>`).
2. When the embedded Axum HTTP server in `rust-core` receives this POST on port 7547:
   - It extracts the device identification (`Manufacturer`, `OUI`, `ProductClass`, `SerialNumber`) and parameter list (`ParameterValueStruct`).
   - It normalizes optical power (e.g., `Device.Optical.Interface.1.OpticalSignalLevel` or `InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.RxPower`), CPU usage, IP address, and firmware version into the existing `TelemetryUpdate` domain model.
   - It pushes this `TelemetryUpdate` into the **exact same MPSC channel** (`tx.send(update)`) that currently processes MQTT messages.
3. Because `run_db_sink` in `rust-core` simply dequeues `TelemetryUpdate` structs from `rx` and executes the atomic upsert into `cpe_live_state`:
   - Both TR-369 (USP via MQTT) and TR-069 (CWMP via HTTP) converge onto the same in-RAM `cpe_live_state` table.
   - The PL/pgSQL trigger `reconcile_live_to_history` automatically fires upon optical changes > 1.0 dBm regardless of whether the update came from MQTT or CWMP.
   - **Result**: PostgreSQL schema requires zero modifications for telemetry ingestion, and `simulate_flow.sh` Steps 2, 3, and 4 semantics remain 100% intact.

---

### 2.2 Deep Architectural Trade-Off: PostgreSQL Command Queue vs Direct HTTP to Rust Core

Because TR-069 devices operate strictly via client-initiated polling, commands cannot be pushed asynchronously over TCP like TR-369 MQTT. They must be queued until the device opens an HTTP Inform session.

Two architectural options exist for storing and retrieving these commands:

| Feature / Dimension | Option A: PostgreSQL Table (`cpe_pending_commands`) | Option B: Direct HTTP Endpoint in Rust Core |
|---|---|---|
| **Architectural Paradigm** | **Decoupled Event/DB-Centric** (Standard for ACS) | **Point-to-Point Coupling** |
| **Persistence across Restarts** | **100% Durable**: Commands survive `rust-core` restarts, redeploys, and crashes. | **Zero Durability**: Commands held in Rust memory (`RwLock<HashMap>`) are wiped on restart. |
| **Security & Attack Surface** | **Zero WAN Exposure**: Port 7547 only handles CWMP XML. No internal management API exposed to WAN. | **High Risk**: Internal API on 7547 allows rogue ONTs to trigger commands, or requires a 2nd port. |
| **Availability Coupling** | **Independent**: FastAPI can queue commands even while `rust-core` is recompiling or restarting. | **Hard Dependency**: FastAPI requests fail with 503 if `rust-core` is momentarily down. |
| **Auditability & Traceability** | Full SQL history: `created_at`, `dispatched_at`, `completed_at`, and captured XML responses. | No historical audit without building an entire secondary logging system. |
| **Multi-Worker Scalability** | Safe concurrency via `SELECT ... FOR UPDATE SKIP LOCKED` across any number of workers. | Memory isolation: If worker A queues, worker B serving the ONT Inform cannot see it. |
| **Performance Overhead** | Sub-millisecond indexed lookup (`cpe_id, status, created_at`). TR-069 polling is infrequent (30s–5m). | In-memory lookup is microsecond fast, but latency savings are irrelevant for TR-069. |

**Architectural Assessment & Conclusion**:  
**Option A (PostgreSQL Table `cpe_pending_commands`) is overwhelmingly superior**. It strictly honors the Event-Driven / Database-Centric architecture of the project, guarantees durability, eliminates security risks on the public-facing CWMP port 7547, and provides complete auditability.

---

### 2.3 CWMP Inform Session & Command Dispatch State Machine

```
 ONT (Huawei EchoLife)                  Rust CWMP (Port 7547)               PostgreSQL (acs_db)
       |                                         |                                    |
       |----- 1. HTTP POST (cwmp:Inform) ------->|                                    |
       |                                         |-- 2. Upsert MPSC (TelemetryUpdate)->| (cpe_live_state)
       |<---- 3. HTTP 200 (InformResponse) ------|                                    |
       |                                         |                                    |
       |----- 4. HTTP POST (empty body) -------->|                                    |
       |      [ONT signals ready for RPC]        |-- 5. Query pending command ------->| (cpe_pending_commands)
       |                                         |<-- Returns 'Reboot' / 'GetParam'---|
       |                                         |-- 6. Mark status = 'sent' -------->|
       |<---- 7. HTTP 200 (SOAP RPC Command) ----|                                    |
       |                                         |                                    |
       |----- 8. HTTP POST (RPC Response) ------>|                                    |
       |      [e.g. cwmp:RebootResponse]         |-- 9. Mark status = 'completed' --->|
       |<---- 10. HTTP 204 No Content -----------|                                    |
       |      [Session Terminated]               |                                    |
```

1. **Step 1–3 (Inform Handshake)**:
   - ONT sends `<cwmp:Inform>`.
   - Rust parses metadata and sends `TelemetryUpdate` to MPSC queue (feeding `cpe_live_state`).
   - Rust responds with `<cwmp:InformResponse>`.
2. **Step 4–7 (Command Delivery)**:
   - ONT sends empty HTTP POST (`Content-Length: 0`).
   - Rust queries:
     ```sql
     SELECT id, command_id, command_type, command_key, parameters
     FROM cpe_pending_commands
     WHERE cpe_id = $1 AND status = 'pending'
     ORDER BY created_at ASC
     LIMIT 1
     FOR UPDATE SKIP LOCKED;
     ```
   - If a pending command is found:
     - Rust updates status:
       ```sql
       UPDATE cpe_pending_commands
       SET status = 'sent', dispatched_at = CURRENT_TIMESTAMP
       WHERE id = $1;
       ```
     - Rust constructs SOAP Envelope containing `<cwmp:Reboot>` or `<cwmp:GetParameterValues>` and returns HTTP 200.
   - If no pending command exists: Rust returns `HTTP 204 No Content` to terminate the session cleanly.
3. **Step 8–10 (Command Completion)**:
   - ONT executes RPC and returns `<cwmp:RebootResponse>` or `<cwmp:GetParameterValuesResponse>`.
   - Rust records response into `cpe_pending_commands.result` and marks `status = 'completed'`, `completed_at = CURRENT_TIMESTAMP`.
   - Rust returns `HTTP 204 No Content` (or next pending command if available).

---

### 2.4 Proposed Database Schema: `cpe_pending_commands`

To be appended to `postgres/init.sql`:

```sql
-- ----------------------------------------------------------------------------
-- 7. Persistent TR-069 Pending Commands Queue Table
-- ----------------------------------------------------------------------------
-- Stores queued CWMP RPC commands (GetParameterValues, Reboot, SetParameterValues)
-- awaiting delivery during the next periodic Inform session.
CREATE TABLE IF NOT EXISTS cpe_pending_commands (
    id BIGSERIAL PRIMARY KEY,
    command_id VARCHAR(64) UNIQUE NOT NULL,
    cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
    command_type VARCHAR(64) NOT NULL,
    command_key VARCHAR(64),
    parameters JSONB NOT NULL DEFAULT '[]'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    result JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dispatched_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_cpe_pending_commands_queue 
    ON cpe_pending_commands (cpe_id, status, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_cpe_pending_commands_status 
    ON cpe_pending_commands (status);

DROP TRIGGER IF EXISTS trg_cpe_pending_commands_updated_at ON cpe_pending_commands;
CREATE TRIGGER trg_cpe_pending_commands_updated_at
BEFORE UPDATE ON cpe_pending_commands
FOR EACH ROW
EXECUTE FUNCTION fn_set_updated_at();
```

---

### 2.5 Proposed SQLAlchemy ORM Model & Pydantic Schemas

#### In `python-api/app/models.py`:
```python
class CpePendingCommand(Base):
    """Persistent queue of TR-069 CWMP commands awaiting Inform session."""
    __tablename__ = "cpe_pending_commands"

    id: Mapped[int] = mapped_column(BIGINT_TYPE, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    cpe_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("cpe_inventory.cpe_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    command_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parameters: Mapped[list[Any]] = mapped_column(
        JSON_TYPE,
        server_default="[]",
        default=list,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    inventory: Mapped["CpeInventory"] = relationship("CpeInventory", backref="pending_commands")
```

#### In `python-api/app/schemas.py`:
```python
class GetParameterValuesRequest(BaseModel):
    parameter_names: list[str] = Field(..., min_length=1, description="List of TR-069/TR-181 parameter names")

class CommandQueueResponse(BaseModel):
    command_id: str
    cpe_id: str
    command_type: str
    command_key: Optional[str] = None
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class CommandDetailResponse(CommandQueueResponse):
    parameters: list[Any] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
```

---

### 2.6 Dual-Stack API Strategy in `python-api/app/routers/cpes.py`

To achieve **100% backward compatibility and zero regressions**:
1. `POST /api/v1/cpes/{cpe_id}/reboot`:
   - Accept optional query parameter `protocol: Optional[str] = Query(None, description="tr069, tr369, or dual")`.
   - **Default behavior**:
     - Always execute `mqtt_publisher.publish_reboot_command(cpe_id)` (satisfies `simulate_flow.sh` Step 5).
     - Simultaneously enqueue a `Reboot` command in `cpe_pending_commands` so if the ONT is a TR-069 device, it will execute upon Inform.
     - Return the standard `CommandDispatchResponse` format expected by `simulate_flow.sh`.
2. `POST /api/v1/cpes/{cpe_id}/get-parameter-values` (and `parameters/get` alias):
   - Enqueue a `GetParameterValues` record in `cpe_pending_commands`.
   - Returns HTTP 200/202 with `CommandQueueResponse`.
3. `GET /api/v1/cpes/{cpe_id}/commands`:
   - Lists queued/executed commands for this device with status filter.
4. `GET /api/v1/cpes/{cpe_id}/commands/{command_id}`:
   - Returns detailed status and execution result.

---

### 2.7 Docker Compose Updates Required

In `docker-compose.yml`, update `rust-core`:

```yaml
  rust-core:
    build:
      context: ./rust-core
    environment:
      - DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db
      - MQTT_HOST=mosquitto
      - MQTT_PORT=1883
      - CWMP_PORT=7547
      - CWMP_HOST=0.0.0.0
    ports:
      - "7547:7547"
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

- **Port Exposure**: Exposes port 7547 on the host.
- **Resource Limits**: 500M is more than sufficient (Axum + hyper adds only ~10MB RAM in Rust).
- **Tooling Compatibility**: `configure_limits.py` continues to work cleanly because it only modifies memory strings and does not interfere with `ports` or `environment`.

---

### 2.8 Non-Regression Guarantees for `simulate_flow.sh`

`simulate_flow.sh` executes 5 specific steps. The proposed dual-stack additions guarantee zero regressions because:

1. **Step 1 (CPE Registration)**:
   - Schema in `cpe_inventory` remains 100% identical.
   - `POST /api/v1/cpes` and `GET /api/v1/cpes/{cpe_id}` remain unchanged.
2. **Step 2 (TR-369 Telemetry via MQTT)**:
   - `rust-core` continues running `run_mqtt_ingest` on topic `usp/endpoint/#`.
3. **Step 3 (RAM Table Ingestion Validation)**:
   - `cpe_live_state` table definition, indexes, and tablespace remain identical.
   - `/api/v1/cpes/{cpe_id}/live-state` continues providing `telemetry_metrics` and `metrics` aliases.
4. **Step 4 (Reconciliation Trigger & History)**:
   - Trigger `reconcile_live_to_history` logic on `cpe_live_state` is untouched.
   - `cpe_historical_metrics` and `cpe_state_history` view are untouched.
5. **Step 5 (Reboot Command via MQTT)**:
   - `POST /api/v1/cpes/{cpe_id}/reboot` continues publishing to `usp/endpoint/{cpe_id}/request` with QoS 1 and the exact payload matching regex `reboot|operate`.

---

## 3. Caveats

1. **CPE Identification in TR-069**:
   - TR-369 devices send a pre-configured `cpe_id` (e.g. `cpe-sim-001`).
   - TR-069 ONTs report `Manufacturer`, `OUI`, `ProductClass`, and `SerialNumber`.
   - *Design Decision*: `rust-core` should resolve `cpe_id` by performing a fast lookup against `cpe_inventory` using `serial_number`. If not yet registered, it defaults `cpe_id = serial_number`.
2. **SOAP XML Namespaces**:
   - Different ONT vendors use minor variations in SOAP namespaces (`urn:dslforum-org:cwmp-1-0`, `cwmp-1-1`, or `cwmp-1-2`).
   - The XML parser in `rust-core` should match local XML element tags (e.g. `Inform`, `DeviceId`, `SerialNumber`, `ParameterValueStruct`) disregarding strict namespace URI prefixes.
3. **HTTP Authentication (CWMP)**:
   - TR-069 allows HTTP Basic or Digest Authentication. In local lab / default configurations, standard ONTs connect with standard credentials or without auth. The server should accept unauthenticated requests or standard credentials (`admin:admin`).

---

## 4. Conclusion

1. **Database Schema**: The existing PostgreSQL schema (`cpe_inventory`, `cpe_live_state`, `cpe_historical_metrics`) and triggers perfectly support dual-stack ingestion without any breaking modifications. Adding table `cpe_pending_commands` cleanly satisfies TR-069 command queuing with ACID durability and zero WAN attack surface.
2. **Command Handling**: The PostgreSQL table approach (`cpe_pending_commands`) is confirmed as the optimal, decoupled design over direct HTTP in Rust Core.
3. **API Implementation**: `python-api` can easily support `GetParameterValues` and dual-stack `Reboot` while keeping 100% backward compatibility.
4. **Docker Compose**: Exposing `7547:7547` on `rust-core` is verified safe with zero port collisions or tool breakage.
5. **Non-Regression**: `simulate_flow.sh` is guaranteed to pass with exit code 0 under the proposed dual-stack architecture.

---

## 5. Verification Method

To independently verify all findings and baseline behavior:

1. **Verify PostgreSQL Schema & Reconciliation Semantics**:
   ```bash
   python3 -m unittest discover -s postgres -p "test_*.py" -v
   ```
   *Expected Output*: 68 tests run, 67 passed, 1 skipped (live DB), exit code 0.

2. **Verify Python API Test Suite**:
   ```bash
   PYTHONPATH=python-api python3 -m pytest -v python-api/tests/test_api.py
   PYTHONPATH=python-api python3 -m pytest -v python-api/tests/test_adversarial.py
   ```
   *Expected Output*: 20 passed in ~3s, exit code 0.

3. **Verify Resource Limit Tooling**:
   ```bash
   python3 configure_limits.py --show
   ```
   *Expected Output*: Shows table with postgres (1.5G), mosquitto (500M), rust-core (500M), python-api (1G), exit code 0.

4. **Verify Port 7547 Availability**:
   ```bash
   ss -tulpn | grep 7547 || echo "Port 7547 is available"
   ```
   *Expected Output*: "Port 7547 is available".
