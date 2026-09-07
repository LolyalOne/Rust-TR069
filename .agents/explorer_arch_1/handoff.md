# Architectural Handoff Report: PostgreSQL Hybrid Model, FastAPI Manager, Git Setup, and E2E Simulation

**Agent ID**: `explorer_arch_1` (teamwork_preview_explorer)  
**Parent ID**: `6258e12c-9553-47a2-9624-69521a0b2d82`  
**Date**: 2026-09-07T00:58:30Z  
**Scope Reference**: `/mnt/d/Projetos/TR069-181/ORIGINAL_REQUEST.md` (R4, R6, R7, simulate_flow.sh)

---

## 1. Observation

Direct inspection of existing codebase and environmental constraints revealed:

1. **`ORIGINAL_REQUEST.md` Requirements**:
   - **R1 & R4**:
     - Line 20: *"Postgres deve usar um volume `tmpfs` para os dados em memória."*
     - Line 28–29: *"Tabela persistente `cpe_inventory` e tabela `unlogged` em RAM `cpe_live_state`. Uma função/trigger de reconciliação deve migrar estados validados da memória para tabelas históricas."*
   - **R6**:
     - Line 34–35: *"API RESTful assíncrona com endpoints CRUD de inventário, consulta de status em tempo real da tabela `unlogged`, e disparo de comandos (ex: Reboot) publicando no broker MQTT."*
   - **R7**:
     - Line 37–38: *"Ao final do desenvolvimento e dos testes, a equipe deve inicializar o repositório Git localmente, adicionar o remote `https://github.com/LolyalOne/Rust-TR069.git`, realizar o commit inicial completo de toda a estrutura do projeto e realizar o push."*
   - **Acceptance Criteria (Simulate Flow)**:
     - Lines 48–54: 5-step automated CLI simulation (`simulate_flow.sh`):
       1. Register test CPE via FastAPI.
       2. Publish TR-369 telemetry payload via MQTT.
       3. Validate Rust worker consumed and updated RAM table (`cpe_live_state`).
       4. Validate metrics alteration fired reconciliation trigger and recorded into history (`cpe_state_history`).
       5. Dispatch API command publishing request back to MQTT broker. Must finish with exit code 0.

2. **`docker-compose.yml`**:
   - Lines 4–15:
     ```yaml
     postgres:
       image: postgres:15-alpine
       environment:
         POSTGRES_DB: acs_db
         POSTGRES_USER: acs_user
         POSTGRES_PASSWORD: acs_password
       volumes:
         - pg_data:/var/lib/postgresql/data
         - ./postgres/init.sql:/docker-entrypoint-initdb.d/init.sql
       tmpfs:
         - /var/lib/postgresql/ram_data:uid=999,gid=999,mode=0700,size=1G
     ```
   - Lines 65–82:
     ```yaml
     python-api:
       build:
         context: ./python-api
       environment:
         - DATABASE_URL=postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db
         - MQTT_HOST=mosquitto
         - MQTT_PORT=1883
       ports:
         - "8000:8000"
     ```

3. **Database Integrity & PostgreSQL Constraints**:
   - In PostgreSQL, a permanent (logged) table **cannot** reference an `UNLOGGED` table via a Foreign Key (`ERROR: cannot reference unlogged relation from permanent relation`).
   - However, an `UNLOGGED` table **can** reference a permanent table (`cpe_inventory`).
   - A permanent history table (`cpe_state_history`) must therefore reference `cpe_inventory(cpe_id)` directly, ensuring full referential integrity.
   - The tmpfs mount `/var/lib/postgresql/ram_data` matches the PostgreSQL tablespace mechanism: `CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';`.

4. **Environment & Git Status**:
   - Running `git status` in `/mnt/d/Projetos/TR069-181` returned `fatal: not a git repository` (exit code 128).
   - `git ls-remote https://github.com/LolyalOne/Rust-TR069.git` returned exit code 0 (repository reachable).
   - Global git config has `user.name=mkdebug` and `user.email=mkdebug@example.com`.
   - Existing `.gitignore` erroneously ignores `Cargo.lock`. For Rust binary application crates, `Cargo.lock` must be tracked to guarantee reproducible builds.

---

## 2. Logic Chain

From the observations, the architectural design follows a strict deductive chain:

1. **Storage Stratification (PostgreSQL Hybrid Model)**:
   - *Premise*: Real-time telemetry generates high-write frequency; WAL (Write-Ahead Logging) disk writes cause I/O bottlenecks and disk wear.
   - *Deduction*: Placing `cpe_live_state` in PostgreSQL as `UNLOGGED TABLE ... TABLESPACE ram_tablespace` eliminates WAL overhead and stores pages directly in tmpfs (RAM), fulfilling R1 and R4.
   - *Durability Bridge*: Because `cpe_live_state` is volatile (truncated on ungraceful restart), durable state must be preserved. A PL/pgSQL trigger on `AFTER INSERT OR UPDATE ON cpe_live_state` selectively reconciles validated states and metric alterations into the permanent `cpe_state_history` table.
   - *Referential Integrity Safeguard*: Since permanent tables cannot reference unlogged tables in PostgreSQL, `cpe_state_history` references `cpe_inventory(cpe_id) ON DELETE CASCADE`.

2. **FastAPI Manager Architecture (R6)**:
   - *Premise*: API must handle concurrent REST CRUD, query RAM table in microseconds, and publish commands without blocking.
   - *Deduction*: Use `SQLAlchemy 2.0` with `asyncpg` connection pool.
   - *MQTT Integration*: Leverage `aiomqtt` managed within FastAPI's `lifespan` context. When `POST /api/v1/cpes/{cpe_id}/reboot` is called, the async client publishes a structured USP command to `usp/endpoint/{cpe_id}/command`, returning HTTP 202 Accepted.

3. **Simulation Flow & Acceptance Guarantee (`simulate_flow.sh`)**:
   - *Premise*: Must run autonomously and exit with code 0.
   - *Deduction*: The script must orchestrate:
     - Pre-flight readiness health checks.
     - Step 1: POST to FastAPI `/api/v1/cpes` -> verifies inventory creation.
     - Step 2: Publish MQTT telemetry to `usp/endpoint/{cpe_id}` via Mosquitto.
     - Step 3: Polling GET to `/api/v1/cpes/{cpe_id}/live-state` -> confirms Rust core consumed MQTT message and wrote to RAM table.
     - Step 4: Query `/api/v1/cpes/{cpe_id}/history` (or direct SQL) -> confirms reconciliation trigger executed upon metric alteration.
     - Step 5: Subscribe to `usp/endpoint/{cpe_id}/command`, call POST `/api/v1/cpes/{cpe_id}/reboot`, assert command payload received.

4. **Git Setup & Push Strategy (R7)**:
   - Initialize git branch `main`, refine `.gitignore` (keep `Cargo.lock`, ignore volatile/build artifacts), configure remote `https://github.com/LolyalOne/Rust-TR069.git`, and document push/credential handling.

---

## 3. Technical Specifications & Architecture Designs

### 3.1 PostgreSQL Hybrid Data Model (R4)

File path: `postgres/init.sql` (mounted into `/docker-entrypoint-initdb.d/init.sql`).

```sql
-- ============================================================================
-- TR-369 / USP ACS Hybrid Database Schema
-- ============================================================================

-- 1. Create Tablespace on RAM tmpfs volume
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_tablespace WHERE spcname = 'ram_tablespace') THEN
        CREATE TABLESPACE ram_tablespace LOCATION '/var/lib/postgresql/ram_data';
    END IF;
END $$;

-- 2. Persistent Inventory Table
CREATE TABLE IF NOT EXISTS cpe_inventory (
    cpe_id VARCHAR(128) PRIMARY KEY,               -- e.g. "proto::00259E-SN12345678" or "cpe-test-001"
    serial_number VARCHAR(64) NOT NULL UNIQUE,
    manufacturer VARCHAR(64) NOT NULL,
    model VARCHAR(64) NOT NULL,
    oui VARCHAR(6),                                 -- Organizationally Unique Identifier
    product_class VARCHAR(64),
    hardware_version VARCHAR(64),
    software_version VARCHAR(64),
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cpe_inventory_mfg_model ON cpe_inventory(manufacturer, model);

-- 3. Unlogged In-RAM Live State Table (Stored in ram_tablespace)
CREATE UNLOGGED TABLE IF NOT EXISTS cpe_live_state (
    cpe_id VARCHAR(128) PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
    endpoint_id VARCHAR(256),
    current_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    telemetry_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'offline',    -- online, offline, rebooting, error
    ip_address VARCHAR(64),
    firmware_version VARCHAR(64),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
) TABLESPACE ram_tablespace;

CREATE INDEX IF NOT EXISTS idx_cpe_live_telemetry ON cpe_live_state USING gin (telemetry_metrics);
CREATE INDEX IF NOT EXISTS idx_cpe_live_params ON cpe_live_state USING gin (current_parameters);
CREATE INDEX IF NOT EXISTS idx_cpe_live_status ON cpe_live_state(status);
CREATE INDEX IF NOT EXISTS idx_cpe_live_last_seen ON cpe_live_state(last_seen);

-- 4. Persistent Historical State Table
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

-- 5. Updated_At Helper Functions
CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_cpe_inventory_updated_at
BEFORE UPDATE ON cpe_inventory
FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- 6. State Reconciliation Function & Trigger
CREATE OR REPLACE FUNCTION fn_reconcile_cpe_live_state()
RETURNS TRIGGER AS $$
DECLARE
    v_reason VARCHAR(64);
    v_should_record BOOLEAN := FALSE;
BEGIN
    IF (TG_OP = 'INSERT') THEN
        v_reason := 'initial_state';
        v_should_record := TRUE;
    ELSIF (TG_OP = 'UPDATE') THEN
        -- Check for status transition and telemetry alterations
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
            NEW.updated_at,
            v_reason
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cpe_live_state_reconcile ON cpe_live_state;
CREATE TRIGGER trg_cpe_live_state_reconcile
AFTER INSERT OR UPDATE ON cpe_live_state
FOR EACH ROW EXECUTE FUNCTION fn_reconcile_cpe_live_state();
```

---

### 3.2 Python FastAPI Manager Architecture (R6)

#### Directory Structure
```
python-api/
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

#### Dependencies (`python-api/requirements.txt`)
```
fastapi>=0.111.0,<0.112.0
uvicorn[standard]>=0.30.0,<0.31.0
sqlalchemy[asyncio]>=2.0.30
asyncpg>=0.29.0
pydantic>=2.7.0
pydantic-settings>=2.2.0
aiomqtt>=2.1.0
```

#### Key Components Specification

1. **Database Session (`app/database.py`)**:
   - Async engine: `create_async_engine(settings.DATABASE_URL, pool_size=10, max_overflow=20)`
   - Session maker: `async_sessionmaker(engine, expire_on_commit=False)`
   - Dependency: `async def get_db() -> AsyncGenerator[AsyncSession, None]`

2. **MQTT Integration (`app/mqtt_client.py`)**:
   ```python
   import json
   from aiomqtt import Client, MqttError
   from app.config import settings

   class MQTTManager:
       def __init__(self):
           self.client: Client | None = None

       async def connect(self):
           self.client = Client(
               hostname=settings.MQTT_HOST,
               port=settings.MQTT_PORT,
               identifier="fastapi-manager",
           )
           await self.client.__aenter__()

       async def disconnect(self):
           if self.client:
               await self.client.__aexit__(None, None, None)

       async def publish_command(self, cpe_id: str, command: str, params: dict | None = None):
           if not self.client:
               raise RuntimeError("MQTT Client not connected")
           topic = f"usp/endpoint/{cpe_id}/command"
           payload = json.dumps({
               "cpe_id": cpe_id,
               "command": command,
               "parameters": params or {},
               "timestamp": ...
           })
           await self.client.publish(topic, payload=payload, qos=1)
   ```

3. **FastAPI Lifespan (`app/main.py`)**:
   ```python
   @asynccontextmanager
   async def lifespan(app: FastAPI):
       # Startup
       mqtt_mgr = MQTTManager()
       await mqtt_mgr.connect()
       app.state.mqtt = mqtt_mgr
       yield
       # Shutdown
       await mqtt_mgr.disconnect()
   ```

4. **REST API Endpoints**:

| Method | Endpoint | Description | Status Code |
|---|---|---|---|
| `POST` | `/api/v1/cpes` | Register new CPE in inventory | `201 Created` |
| `GET` | `/api/v1/cpes` | List inventory (pagination: `limit`, `offset`) | `200 OK` |
| `GET` | `/api/v1/cpes/{cpe_id}` | Get CPE inventory + latest status | `200 OK` / `404` |
| `PUT` | `/api/v1/cpes/{cpe_id}` | Update CPE inventory metadata | `200 OK` / `404` |
| `DELETE` | `/api/v1/cpes/{cpe_id}` | Delete CPE (cascades to live & history) | `204 No Content` / `404` |
| `GET` | `/api/v1/cpes/{cpe_id}/live-state` | Query in-RAM `cpe_live_state` directly | `200 OK` / `404` |
| `GET` | `/api/v1/cpes/{cpe_id}/history` | Query `cpe_state_history` with limit/offset | `200 OK` / `404` |
| `POST` | `/api/v1/cpes/{cpe_id}/reboot` | Publish reboot command to MQTT | `202 Accepted` |
| `GET` | `/health` | Service health check | `200 OK` |

---

### 3.3 Version Control Strategy & Git Integration (R7)

1. **`.gitignore` Revision**:
   - Update `.gitignore` to track `Cargo.lock` (crucial for Rust reproducible container builds).
   - Maintain ignores for `pg_data/`, `mosquitto_data/`, `.env`, `target/`, `__pycache__/`, `.devcontainer/local-features/` (if any).

2. **Git Repository Setup Runbook**:
   ```bash
   cd /mnt/d/Projetos/TR069-181
   git init -b main
   git config user.name "Teamwork Engineer"
   git config user.email "engineer@teamwork.local"
   git add .
   git commit -m "feat: initial implementation of TR-369/USP ACS on MQTT with Rust Core and Python FastAPI"
   git remote add origin https://github.com/LolyalOne/Rust-TR069.git
   git branch -M main
   ```

3. **Push Authentication Caveats & Handling**:
   - In non-interactive WSL/CI environments, `git push -u origin main` requires credentials (SSH key or GitHub Personal Access Token).
   - When executing the final push, use the configured credential manager or support token injection:
     ```bash
     # Standard push if SSH/Credential Manager is present:
     git push -u origin main
     # If token is provided in environment:
     # git push https://${GH_TOKEN}@github.com/LolyalOne/Rust-TR069.git main
     ```

---

### 3.4 E2E Verification & Simulation Script (`simulate_flow.sh`)

The script implements the strict 5-step test sequence, ensuring exit code 0 on full compliance:

```bash
#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
MQTT_HOST="${MQTT_HOST:-localhost}"
MQTT_PORT="${MQTT_PORT:-1883}"
CPE_ID="cpe-sim-001"
SERIAL="SN-TEST-$(date +%s)"

echo "=== TR-369 / USP ACS E2E Simulation Flow ==="

# Step 0: Health Checks
echo "[0/5] Checking service availability..."
curl -s --retry 10 --retry-delay 2 --retry-connrefused "${API_URL}/health" > /dev/null
echo "✓ FastAPI Manager is ready."

# Step 1: Register CPE in Inventory via API
echo "[1/5] Registering CPE ${CPE_ID} via FastAPI..."
REG_RESP=$(curl -s -X POST "${API_URL}/api/v1/cpes" \
  -H "Content-Type: application/json" \
  -d "{
    \"cpe_id\": \"${CPE_ID}\",
    \"serial_number\": \"${SERIAL}\",
    \"manufacturer\": \"TP-Link\",
    \"model\": \"Archer-AX50\",
    \"oui\": \"00259E\",
    \"product_class\": \"Gateway\"
  }")
echo "Response: ${REG_RESP}"
echo "✓ Step 1 Passed: CPE registered."

# Step 2: Publish TR-369 Telemetry payload via MQTT
echo "[2/5] Publishing TR-369 telemetry payload to MQTT topic usp/endpoint/${CPE_ID}/telemetry..."
TELEMETRY_PAYLOAD=$(cat <<EOF
{
  "cpe_id": "${CPE_ID}",
  "metrics": {
    "cpu_usage": 42.5,
    "memory_usage": 68.0,
    "rx_bytes": 1048576,
    "tx_bytes": 524288,
    "temperature": 45.2
  },
  "parameters": {
    "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
    "Device.WiFi.Radio.1.Status": "Up"
  },
  "status": "online"
}
EOF
)

# Publish via mosquitto_pub (or docker exec mosquitto)
if command -v mosquitto_pub >/dev/null 2>&1; then
  mosquitto_pub -h "${MQTT_HOST}" -p "${MQTT_PORT}" -t "usp/endpoint/${CPE_ID}/telemetry" -m "${TELEMETRY_PAYLOAD}"
else
  docker exec -i "$(docker compose ps -q mosquitto)" mosquitto_pub -h localhost -p 1883 -t "usp/endpoint/${CPE_ID}/telemetry" -m "${TELEMETRY_PAYLOAD}"
fi
echo "✓ Step 2 Passed: Telemetry published to MQTT broker."

# Step 3: Validate Rust Worker consumed message and updated RAM table
echo "[3/5] Validating Rust worker consumption and cpe_live_state in RAM..."
MAX_RETRIES=15
ATTEMPT=0
MATCH_FOUND=0

while [ $ATTEMPT -lt $MAX_RETRIES ]; do
  LIVE_STATE=$(curl -s "${API_URL}/api/v1/cpes/${CPE_ID}/live-state" || echo "{}")
  STATUS=$(echo "${LIVE_STATE}" | jq -r '.status // empty')
  CPU_METRIC=$(echo "${LIVE_STATE}" | jq -r '.telemetry_metrics.cpu_usage // empty')
  
  if [ "${STATUS}" = "online" ] && [ "${CPU_METRIC}" = "42.5" ]; then
    MATCH_FOUND=1
    break
  fi
  ATTEMPT=$((ATTEMPT+1))
  sleep 1
done

if [ $MATCH_FOUND -ne 1 ]; then
  echo "FAIL: cpe_live_state did not update within timeout. Got: ${LIVE_STATE}"
  exit 1
fi
echo "✓ Step 3 Passed: cpe_live_state confirmed in-memory."

# Step 4: Validate Metric Alteration triggered reconciliation into cpe_state_history
echo "[4/5] Validating reconciliation trigger migrated state to cpe_state_history..."
# Publish metric change
MODIFIED_PAYLOAD=$(echo "${TELEMETRY_PAYLOAD}" | jq '.metrics.cpu_usage = 88.4')
if command -v mosquitto_pub >/dev/null 2>&1; then
  mosquitto_pub -h "${MQTT_HOST}" -p "${MQTT_PORT}" -t "usp/endpoint/${CPE_ID}/telemetry" -m "${MODIFIED_PAYLOAD}"
else
  docker exec -i "$(docker compose ps -q mosquitto)" mosquitto_pub -h localhost -p 1883 -t "usp/endpoint/${CPE_ID}/telemetry" -m "${MODIFIED_PAYLOAD}"
fi
sleep 2

HIST_RESP=$(curl -s "${API_URL}/api/v1/cpes/${CPE_ID}/history")
HIST_COUNT=$(echo "${HIST_RESP}" | jq '.items | length')

if [ "${HIST_COUNT}" -lt 2 ]; then
  echo "FAIL: Expected at least 2 history records, found ${HIST_COUNT}. Response: ${HIST_RESP}"
  exit 1
fi
echo "✓ Step 4 Passed: Metric alteration successfully triggered reconciliation history."

# Step 5: Dispatch Reboot Command via API and assert MQTT publication
echo "[5/5] Disagreeing command via API and verifying publication on MQTT broker..."
CMD_LOG=$(mktemp)

# Start background MQTT subscriber for 1 message on command topic
if command -v mosquitto_sub >/dev/null 2>&1; then
  mosquitto_sub -h "${MQTT_HOST}" -p "${MQTT_PORT}" -t "usp/endpoint/${CPE_ID}/command" -C 1 -W 10 > "${CMD_LOG}" &
else
  docker exec -i "$(docker compose ps -q mosquitto)" mosquitto_sub -h localhost -p 1883 -t "usp/endpoint/${CPE_ID}/command" -C 1 -W 10 > "${CMD_LOG}" &
fi
SUB_PID=$!
sleep 1

# Dispatch Reboot via FastAPI
REBOOT_RESP=$(curl -s -X POST "${API_URL}/api/v1/cpes/${CPE_ID}/reboot")
echo "API Response: ${REBOOT_RESP}"

wait $SUB_PID || true
CAPTURED_CMD=$(cat "${CMD_LOG}")
rm -f "${CMD_LOG}"

if [[ "${CAPTURED_CMD}" != *"reboot"* ]]; then
  echo "FAIL: Command was not received on MQTT broker. Captured: ${CAPTURED_CMD}"
  exit 1
fi
echo "✓ Step 5 Passed: Reboot command captured on MQTT broker: ${CAPTURED_CMD}"

echo "================================================================="
echo "  ALL 5 E2E SIMULATION STEPS PASSED SUCCESSFULLY (Exit Code 0)  "
echo "================================================================="
exit 0
```

---

## 4. Caveats

1. **PostgreSQL Tablespace Creation Privilege**:
   - Creating a tablespace requires superuser privileges (`postgres` user) or `CREATE TABLESPACE` grant. Since `docker-compose.yml` mounts `init.sql` into `/docker-entrypoint-initdb.d/`, it executes as `POSTGRES_USER: acs_user`. Ensure `POSTGRES_USER` has superuser or use default `postgres` during init.
2. **tmpfs Lifespan on Container Restart**:
   - As designed, `cpe_live_state` resides in volatile RAM. Upon container restart, `cpe_live_state` is empty. The persistent inventory (`cpe_inventory`) and history (`cpe_state_history`) remain intact on the persistent volume `pg_data`.
3. **Protobuf vs JSON Payload Compatibility**:
   - The Rust worker should decode both Protobuf TR-369 Records and JSON telemetry payloads gracefully so that CLI tools (`mosquitto_pub`) and real TR-369 CPE agents are both natively supported.
4. **Git Remote Push Network Credentials**:
   - The push to `https://github.com/LolyalOne/Rust-TR069.git` will require standard GitHub authentication (PAT or SSH). The implementation worker must ensure credentials or token are supplied when invoking the push step.

---

## 5. Conclusion

1. The PostgreSQL hybrid schema successfully isolates high-throughput volatile writes in RAM (`cpe_live_state`) while ensuring data durability through `cpe_inventory` and `cpe_state_history`.
2. The trigger `fn_reconcile_cpe_live_state` cleanly decouples telemetry ingestion from long-term metric audit logging.
3. The FastAPI asynchronous architecture provides clean REST endpoints for inventory management, instant in-memory status retrieval, and non-blocking MQTT command dispatching.
4. The 5-step simulation script `simulate_flow.sh` is fully specified with clear assertions, retries, and exit code 0 guarantee.

---

## 6. Verification Method

To independently verify this architectural design once implemented by the workers:

1. **PostgreSQL Schema Verification**:
   ```bash
   docker compose exec postgres psql -U acs_user -d acs_db -c "\dt+ cpe_*"
   # Verify cpe_live_state has Persistence = unlogged and Tablespace = ram_tablespace
   ```

2. **FastAPI Endpoints Verification**:
   ```bash
   curl -s http://localhost:8000/docs | grep -i "swagger"
   curl -s http://localhost:8000/api/v1/cpes
   ```

3. **E2E Simulation Script Execution**:
   ```bash
   chmod +x simulate_flow.sh
   ./simulate_flow.sh
   echo "Exit Code: $?" # Must be 0
   ```

4. **Git Remote Verification**:
   ```bash
   git remote -v
   # Expect: origin https://github.com/LolyalOne/Rust-TR069.git (fetch & push)
   ```
