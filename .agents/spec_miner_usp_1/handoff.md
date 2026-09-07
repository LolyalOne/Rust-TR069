# TR-369 / USP ACS Protocol & Worker Specification Report

## 1. Observation

Direct observations from project files, specifications, and environment probes:

1. **User Requirements (`ORIGINAL_REQUEST.md`)**:
   - Line 11: *"Desenvolver e refinar a arquitetura fundacional e o código-base de um ACS (Auto Configuration Server) TR-369/USP operando sobre MQTT. O sistema deve ser tolerante a falhas, assíncrono e estruturado sob o paradigma de Event-Driven Architecture, consistindo de PostgreSQL, Eclipse Mosquitto, Rust USP Core Worker e Python FastAPI Manager."*
   - Line 20 (R1): Memory limits: Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB), Python FastAPI (1 GB). Postgres requires `tmpfs` volume for in-memory data (`/var/lib/postgresql/ram_data`).
   - Line 29 (R4): *"Tabela persistente `cpe_inventory` e tabela `unlogged` em RAM `cpe_live_state`. Uma função/trigger de reconciliação deve migrar estados validados da memória para tabelas históricas."*
   - Line 32 (R5): *"Serviço assíncrono (tokio, rumqttc, sqlx) processando mensagens do broker (`usp/endpoint/#`) e gravando estados no banco de dados usando MPSC Channels para desacoplamento. Para a decodificação de payloads via Protobuf (`prost`), a equipe deve baixar os arquivos `.proto` oficiais diretamente do repositório da Broadband Forum (BBF) ou criar um `.proto` de mock mínimo que simule o padrão USP."*
   - Line 35 (R6): *"API RESTful assíncrona com endpoints CRUD de inventário, consulta de status em tempo real da tabela `unlogged`, e disparo de comandos (ex: Reboot) publicando no broker MQTT."*
   - Lines 48–54: Verification flow requiring automated CLI simulation (`simulate_flow.sh`):
     1. Register test CPE via FastAPI.
     2. Publish TR-369 telemetry via MQTT (Mosquitto).
     3. Validate Rust worker updates RAM table (`cpe_live_state`).
     4. Validate reconciliation trigger persists metrics to historical table (`cpe_state_history`).
     5. Dispatch command via API that publishes request back to MQTT broker.

2. **Broker Configuration (`mosquitto/mosquitto.conf`)**:
   - `listener 1883`, `allow_anonymous true`, `persistence true`, `persistence_location /mosquitto/data/`.
   - Anonymous access is enabled for development/internal communication between containers on `acs_network`.

3. **Current Stub in `bootstrap.sh` (Lines 9–17)**:
   ```protobuf
   syntax = "proto3";
   package usp.record;
   message Record {
       string version = 1;
       string to_id = 2;
       string from_id = 3;
   }
   ```
   *Observation*: This initial stub is missing the outer envelope record types (`record_type.no_session_context.payload`), header fields, and inner message structures (`usp.Msg`, `Header`, `Body`, `Request`, `Response`, `Notify`, `Operate`). It cannot deserialize real TR-369 payloads without complete definition.

4. **Broadband Forum (BBF) Official Specifications**:
   - Official URLs:
     - `https://usp.technology/specification/usp-record-1-3.proto` (131 lines)
     - `https://usp.technology/specification/usp-msg-1-3.proto` (594 lines)
   - Record Structure (`usp_record.Record`):
     - `version = 1`, `to_id = 2`, `from_id = 3`, `payload_security = 4`, `mac_signature = 5`, `sender_cert = 6`
     - `oneof record_type { NoSessionContextRecord no_session_context = 7; ... }`
     - `NoSessionContextRecord`: `bytes payload = 2` (contains serialized `usp.Msg`).
   - Message Structure (`usp.Msg`):
     - `Header header = 1`: `msg_id = 1`, `msg_type = 2` (`GET`, `NOTIFY`, `SET`, `OPERATE`, etc.)
     - `Body body = 2`: `oneof msg_body { Request request = 1; Response response = 2; Error error = 3; }`
     - `Request.notify`: `subscription_id = 1`, `send_resp = 2`, `oneof notification { Event event = 3; ValueChange value_change = 4; ... }`
     - `Request.operate`: `command = 1`, `command_key = 2`, `send_resp = 3`, `input_args = 4`

---

## 2. Logic Chain

1. **Protocol Envelope & Demultiplexing**:
   - TR-369 encapsulates all application messages inside a `Record` envelope.
   - For non-sessionized MQTT communications, `record_type.no_session_context.payload` carries the serialized `usp.Msg`.
   - By matching the field tags of BBF TR-369 (`Record`: 1, 2, 3, 4, 7; `no_session_context`: 2; `Msg`: 1, 2; `Header`: 1, 2; `Body`: 1, 2, 3; `Operate`: 1, 2, 3, 4; `Notify`: 1, 2, 3, 4), any protobuf binary serialized according to this schema is 100% wire-compatible with standard BBF TR-369 implementations (including `obuspa`).

2. **MQTT Topic Hierarchy**:
   - Requirement R5 mandates listening on `usp/endpoint/#`.
   - In standard TR-369 over MQTT:
     - CPE publishes telemetry/notifications/responses on: `usp/endpoint/{endpoint_id}/notify` or `usp/endpoint/{endpoint_id}/response` (or `usp/endpoint/{endpoint_id}`).
     - Controller (FastAPI) dispatches commands to: `usp/endpoint/{endpoint_id}/request`.
     - Rust Worker subscribes to `usp/endpoint/#`. To avoid echo/re-ingestion loops, the Worker inspects either:
       a) The subtopic suffix (ignoring `/request`), OR
       b) The USP Record `to_id` / `from_id` fields (processing only records addressed to Controller).

3. **MPSC Channel Decoupling in Rust Worker**:
   - `rumqttc` runs an asynchronous event loop that processes MQTT keep-alives (PINGREQ/PINGRESP) and incoming packets.
   - If database queries (PostgreSQL writes) are executed inline inside the MQTT event loop, slow queries, lock contention, or transient database disconnections will stall the loop, causing MQTT broker keep-alive timeouts and client disconnects.
   - An asynchronous unbounded or bounded `tokio::sync::mpsc::channel(1024)` decouples the high-throughput MQTT consumer task from the PostgreSQL writer task.
   - The MQTT task decodes protobuf payloads, builds a structured `TelemetryUpdate` event, and sends it down the channel (`tx.send().await`).
   - The DB writer task drains `rx.recv().await` and performs an idempotent `UPSERT` into `cpe_live_state`.

4. **FastAPI Command Dispatch**:
   - When an operator requests `POST /api/v1/devices/{endpoint_id}/reboot`, FastAPI constructs a TR-369 `Operate` request targeting `Device.Reboot()`.
   - It serializes `usp.Msg` into `Record.no_session_context.payload`, serializes `Record` to bytes, and publishes to Mosquitto on `usp/endpoint/{endpoint_id}/request`.
   - This completes the round-trip required by acceptance criteria step 5.

5. **PostgreSQL Hybrid Model & Reconciliation**:
   - `cpe_inventory`: Standard logged table storing registered devices and general metadata.
   - `cpe_live_state`: `UNLOGGED` table in memory (`ram_data` tmpfs), optimizing writes and preventing WAL overhead.
   - `cpe_state_history`: Logged persistent table storing historical time-series snapshots.
   - `reconcile_cpe_live_state()` trigger: Fires `AFTER INSERT OR UPDATE ON cpe_live_state`, updates `cpe_inventory.status = 'ONLINE'` and inserts a historical record into `cpe_state_history`.

---

## 3. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | TR-369 Protocol | USP Record Envelope | Binary outer wrapper containing routing metadata and payload security | `version`, `to_id`, `from_id`, `payload_security`, `record_type` (`no_session_context`) | Encoded Protobuf bytes (`usp_record.Record`) | Schema validation error / Prost `DecodeError` on malformed bytes | BBF TR-369 Spec & `usp-record-1-3.proto` |
| 2 | TR-369 Protocol | USP Msg Envelope | Inner message containing operation header and body | `header` (`msg_id`, `msg_type`), `body` (`request`/`response`/`error`) | Encoded Protobuf bytes (`usp.Msg`) | Error response with `err_code` and `err_msg` | BBF TR-369 Spec & `usp-msg-1-3.proto` |
| 3 | TR-369 Protocol | USP Notify (Telemetry) | CPE asynchronous event or value change notification | `subscription_id`, `send_resp`, `value_change` (`param_path`, `param_value`) or `event` (`obj_path`, `event_name`, `params`) | Parsed parameter map | Discarded if missing payload or corrupted | BBF TR-369 & ORIGINAL_REQUEST R5 |
| 4 | TR-369 Protocol | USP Operate (Reboot) | Remote operation execution command sent by Controller | `command` (e.g. `"Device.Reboot()"`), `command_key`, `send_resp`, `input_args` | Binary Protobuf payload published to MQTT | Returns command failure result if unsupported | BBF TR-369 & ORIGINAL_REQUEST R6 |
| 5 | TR-369 Protocol | USP Get / Set | Read or configure TR-181 parameters remotely | `Get`: `param_paths`, `max_depth`; `Set`: `update_objs` (`param_settings`) | `GetResp` / `SetResp` | `Error` message with `param_errs` | BBF TR-369 Section 6 |
| 6 | MQTT MTP | Agent Ingest Topic | Topic pattern for receiving CPE telemetry and notifications | Topic: `usp/endpoint/{endpoint_id}/notify` or `usp/endpoint/{endpoint_id}` | MQTT PUBLISH packet with binary payload | Filtered by worker if not matching target pattern | TR-369 Section 7.3 & ORIGINAL_REQUEST R5 |
| 7 | MQTT MTP | Controller Dispatch Topic | Topic pattern for sending Controller commands to CPEs | Topic: `usp/endpoint/{endpoint_id}/request` | MQTT PUBLISH packet with binary payload | QoS 1 acknowledgment or disconnect | TR-369 Section 7.3 & ORIGINAL_REQUEST R6 |
| 8 | Rust Core | MPSC Decoupled Pipeline | Internal channel decoupling MQTT packet ingest from DB writes | Incoming MQTT packets | `tokio::sync::mpsc::channel(1024)` passing `TelemetryUpdate` | Backpressure handled via bounded buffer; logs warning on overflow | ORIGINAL_REQUEST R5 |
| 9 | Rust Core | Postgres Live Upsert | Asynchronous writer updating in-memory `cpe_live_state` | `TelemetryUpdate` struct (`endpoint_id`, JSONB parameters, timestamps) | Executed SQL UPSERT on `cpe_live_state` | Reconnection retry with exponential backoff on DB failure | ORIGINAL_REQUEST R4/R5 |
| 10 | FastAPI | CPE Inventory CRUD | REST API endpoints to register, list, and inspect devices | HTTP JSON requests (`POST /api/v1/devices`, `GET /api/v1/devices`) | HTTP 201 Created / 200 OK with device JSON | 404 Not Found if device absent, 400 on duplicate | ORIGINAL_REQUEST R6 |
| 11 | FastAPI | Live State Query | Endpoint returning real-time volatile metrics from RAM table | `GET /api/v1/devices/{endpoint_id}/live` | JSON object with live parameters and `last_contact` | 404 Not Found if device has no live state | ORIGINAL_REQUEST R6 |
| 12 | FastAPI | Historical Metrics Query | Endpoint returning historical state snapshots | `GET /api/v1/devices/{endpoint_id}/history` | JSON array of historical records | Empty list or 404 if no history | ORIGINAL_REQUEST R4/R6 |
| 13 | FastAPI | Command Dispatcher | REST endpoint triggering MQTT command publication | `POST /api/v1/devices/{endpoint_id}/reboot` | Dispatched confirmation JSON + MQTT publish to broker | 503 if MQTT broker unreachable | ORIGINAL_REQUEST R6 |
| 14 | Database | Reconciliation Trigger | PostgreSQL function archiving state and updating status | Trigger on `cpe_live_state` (INSERT or UPDATE) | Row inserted into `cpe_state_history`, status set to 'ONLINE' | Handled inside Postgres transaction | ORIGINAL_REQUEST R4 |

---

## 4. Edge Cases

| # | Feature | Input | Observed / Specified Behavior |
|---|---------|-------|-------------------------------|
| 1 | Protobuf Decoding | Corrupted or non-protobuf byte sequence on `usp/endpoint/test` | `prost::Message::decode` fails with `prost::DecodeError`. Worker catches error, logs warning, and continues loop without crashing. |
| 2 | Topic Filtering | Controller publishes command to `usp/endpoint/cpe-01/request` while worker subscribes to `usp/endpoint/#` | Worker checks topic suffix or `record.to_id`. If topic ends with `/request` (or `to_id != "controller"`), worker ignores it to avoid recursive processing. |
| 3 | Unregistered Device Telemetry | Message arrives from `endpoint_id` that is not present in `cpe_inventory` | `cpe_live_state` has no foreign key constraint or automatically provisions the inventory record with status `ONLINE`. |
| 4 | High-Frequency Telemetry Bursts | CPE publishes 100 value-change messages per second | MPSC channel queues updates. DB writer performs atomic JSONB concatenation (`parameters = cpe_live_state.parameters || EXCLUDED.parameters`) preserving existing keys. |
| 5 | Mosquitto Connection Drop | Mosquitto broker restarts or socket blips | `rumqttc::AsyncClient` event loop catches connection loss, performs automatic backoff retry, and resubscribes to `usp/endpoint/#`. |
| 6 | Database Connection Pool Outage | Postgres temporarily restarts or exceeds connection limits | DB writer task catches `sqlx::Error`, retains message or retries with exponential backoff while MPSC buffer queues pending messages. |
| 7 | Empty or Null Parameters in Msg | USP Msg arrives with empty parameter map or unknown body type | Worker safely skips parameter extraction, updates `last_contact` timestamp, and logs trace event. |
| 8 | Command Dispatch to Offline CPE | FastAPI sends Reboot command to CPE that is disconnected from broker | Mosquitto accepts QoS 1 publish. Message remains queued for persistent session or is discarded per standard MQTT session policy. API returns `DISPATCHED` status. |

---

## 5. Architectural Specifications & Artifact Designs

### 5.1 Protobuf Schema Evaluation & Recommended Specification
Two valid paths exist:
1. **Official BBF Protos**: Fetch `usp-record-1-3.proto` and `usp-msg-1-3.proto` from `https://usp.technology/specification/`.
2. **Self-Contained BBF-Compliant Proto (`usp.proto`)**: A single, clean `.proto` file strictly preserving BBF TR-369 field tag numbers.

**Recommended Solution**:
Use a self-contained, BBF-wire-compatible `proto/usp.proto`. Because Protobuf serialization relies solely on numeric tag IDs and wire types, this schema produces binary payloads identical to official BBF 1.3 schemas, while eliminating ~500 lines of unused legacy types (e.g. STOMP, CoAP, WebSockets, BulkData) and simplifying code generation across both Rust (`prost`) and Python (`protobuf`).

```protobuf
syntax = "proto3";

package usp;

// -------------------------------------------------------------------------
// TR-369 USP Record (Envelope) - BBF Wire-Compatible
// -------------------------------------------------------------------------

message Record {
  string version = 1;
  string to_id = 2;
  string from_id = 3;
  PayloadSecurity payload_security = 4;
  bytes mac_signature = 5;
  bytes sender_cert = 6;

  oneof record_type {
    NoSessionContextRecord no_session_context = 7;
  }

  enum PayloadSecurity {
    PLAINTEXT = 0;
    TLS12 = 1;
  }
}

message NoSessionContextRecord {
  bytes payload = 2; // Serialized usp.Msg
}

// -------------------------------------------------------------------------
// TR-369 USP Msg (Body) - BBF Wire-Compatible
// -------------------------------------------------------------------------

message Msg {
  Header header = 1;
  Body body = 2;
}

message Header {
  string msg_id = 1;
  MsgType msg_type = 2;

  enum MsgType {
    ERROR = 0;
    GET = 1;
    GET_RESP = 2;
    NOTIFY = 3;
    SET = 4;
    SET_RESP = 5;
    OPERATE = 6;
    OPERATE_RESP = 7;
    NOTIFY_RESP = 16;
  }
}

message Body {
  oneof msg_body {
    Request request = 1;
    Response response = 2;
    Error error = 3;
  }
}

message Request {
  oneof req_type {
    Get get = 1;
    Set set = 4;
    Operate operate = 7;
    Notify notify = 8;
  }
}

message Response {
  oneof resp_type {
    GetResp get_resp = 1;
    SetResp set_resp = 4;
    OperateResp operate_resp = 7;
    NotifyResp notify_resp = 8;
  }
}

message Error {
  fixed32 err_code = 1;
  string err_msg = 2;
}

// -------------------------------------------------------------------------
// Operations & Notifications
// -------------------------------------------------------------------------

message Operate {
  string command = 1;         // e.g. "Device.Reboot()"
  string command_key = 2;     // Request tracking UUID
  bool send_resp = 3;
  map<string, string> input_args = 4;
}

message OperateResp {
  repeated OperationResult operation_results = 1;

  message OperationResult {
    string executed_command = 1;
    oneof operation_resp {
      string req_obj_path = 2;
      OutputArgs req_output_args = 3;
      CommandFailure cmd_failure = 4;
    }

    message OutputArgs {
      map<string, string> output_args = 1;
    }

    message CommandFailure {
      fixed32 err_code = 1;
      string err_msg = 2;
    }
  }
}

message Notify {
  string subscription_id = 1;
  bool send_resp = 2;
  oneof notification {
    Event event = 3;
    ValueChange value_change = 4;
  }

  message Event {
    string obj_path = 1;
    string event_name = 2;     // e.g. "Boot!"
    map<string, string> params = 3;
  }

  message ValueChange {
    string param_path = 1;     // e.g. "Device.WiFi.Radio.1.Status"
    string param_value = 2;    // e.g. "Up"
  }
}

message NotifyResp {
  string subscription_id = 1;
}

message Get {
  repeated string param_paths = 1;
  fixed32 max_depth = 2;
}

message GetResp {
  repeated RequestedPathResult req_path_results = 1;

  message RequestedPathResult {
    string requested_path = 1;
    fixed32 err_code = 2;
    string err_msg = 3;
    repeated ResolvedPathResult resolved_path_results = 4;
  }

  message ResolvedPathResult {
    string resolved_path = 1;
    map<string, string> result_params = 2;
  }
}

message Set {
  bool allow_partial = 1;
  repeated UpdateObject update_objs = 2;

  message UpdateObject {
    string obj_path = 1;
    repeated UpdateParamSetting param_settings = 2;
  }

  message UpdateParamSetting {
    string param = 1;
    string value = 2;
    bool required = 3;
  }
}

message SetResp {
  repeated UpdatedObjectResult updated_obj_results = 1;

  message UpdatedObjectResult {
    string requested_path = 1;
  }
}
```

### 5.2 Rust USP Core Worker Architecture
- **Crates**:
  - `tokio = { version = "1.38", features = ["full"] }`
  - `rumqttc = "0.24"`
  - `sqlx = { version = "0.7", features = ["runtime-tokio-rustls", "postgres", "json", "chrono"] }`
  - `prost = "0.12"`
  - `prost-types = "0.12"`
  - `serde = { version = "1.0", features = ["derive"] }`
  - `serde_json = "1.0"`
  - `chrono = { version = "0.4", features = ["serde"] }`
  - `tracing = "0.1"`
  - `tracing-subscriber = { version = "0.3", features = ["env-filter"] }`
  - `dotenvy = "0.15"`
- **Build Script (`build.rs`)**:
  ```rust
  fn main() -> Result<(), Box<dyn std::error::Error>> {
      prost_build::compile_protos(&["proto/usp.proto"], &["proto/"])?;
      Ok(())
  }
  ```
- **Decoupled Processing Design**:
  - `tokio::sync::mpsc::channel::<TelemetryUpdate>(1024)`
  - Task 1 (MQTT Ingest Loop):
    - Connects using `rumqttc::AsyncClient` with client ID `rust-usp-core`.
    - Subscribes to `usp/endpoint/#` (QoS 1).
    - On packet: verifies topic does not end in `/request`.
    - Decodes `Record::decode(&packet.payload[..])`.
    - Decodes `Msg::decode(&record.no_session_context.payload[..])`.
    - Extracts `endpoint_id` from `record.from_id`.
    - Extracts parameter map from `Notify.value_change` or `Notify.event`.
    - Dispatches `TelemetryUpdate` to channel: `tx.send(update).await`.
  - Task 2 (Postgres Sink Loop):
    - Drains `rx.recv().await`.
    - Executes UPSERT on `cpe_live_state`:
      ```sql
      INSERT INTO cpe_live_state (endpoint_id, parameters, last_contact)
      VALUES ($1, $2, NOW())
      ON CONFLICT (endpoint_id) DO UPDATE
      SET parameters = cpe_live_state.parameters || EXCLUDED.parameters,
          last_contact = NOW();
      ```

### 5.3 PostgreSQL Schema & Reconciliation Specification
File: `postgres/init.sql`
```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Persistent device inventory
CREATE TABLE IF NOT EXISTS cpe_inventory (
    id SERIAL PRIMARY KEY,
    endpoint_id VARCHAR(128) UNIQUE NOT NULL,
    serial_number VARCHAR(64),
    manufacturer VARCHAR(64),
    model_name VARCHAR(64),
    mac_address VARCHAR(32),
    firmware_version VARCHAR(64),
    status VARCHAR(32) DEFAULT 'OFFLINE',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Fast in-memory unlogged table for volatile live state
CREATE UNLOGGED TABLE IF NOT EXISTS cpe_live_state (
    endpoint_id VARCHAR(128) PRIMARY KEY,
    parameters JSONB DEFAULT '{}'::jsonb,
    rx_bytes BIGINT DEFAULT 0,
    tx_bytes BIGINT DEFAULT 0,
    cpu_usage NUMERIC(5,2),
    memory_usage NUMERIC(5,2),
    last_contact TIMESTAMPTZ DEFAULT NOW()
);

-- Persistent historical time-series data
CREATE TABLE IF NOT EXISTS cpe_state_history (
    id BIGSERIAL PRIMARY KEY,
    endpoint_id VARCHAR(128) NOT NULL,
    parameters JSONB,
    rx_bytes BIGINT,
    tx_bytes BIGINT,
    cpu_usage NUMERIC(5,2),
    memory_usage NUMERIC(5,2),
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_history_endpoint ON cpe_state_history(endpoint_id, recorded_at);

-- Reconciliation function & trigger
CREATE OR REPLACE FUNCTION reconcile_cpe_live_state()
RETURNS TRIGGER AS $$
BEGIN
    -- Update device status to ONLINE in inventory
    UPDATE cpe_inventory
    SET status = 'ONLINE', updated_at = NEW.last_contact
    WHERE endpoint_id = NEW.endpoint_id;

    -- Snapshot metrics to history
    INSERT INTO cpe_state_history (
        endpoint_id,
        parameters,
        rx_bytes,
        tx_bytes,
        cpu_usage,
        memory_usage,
        recorded_at
    )
    VALUES (
        NEW.endpoint_id,
        NEW.parameters,
        NEW.rx_bytes,
        NEW.tx_bytes,
        NEW.cpu_usage,
        NEW.memory_usage,
        NEW.last_contact
    );

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reconcile_cpe ON cpe_live_state;
CREATE TRIGGER trg_reconcile_cpe
AFTER INSERT OR UPDATE ON cpe_live_state
FOR EACH ROW
EXECUTE FUNCTION reconcile_cpe_live_state();
```

### 5.4 FastAPI Manager Command Dispatch Specification
- **Endpoints**:
  - `POST /api/v1/devices`: Create CPE in `cpe_inventory`.
  - `GET /api/v1/devices`: List all registered CPEs.
  - `GET /api/v1/devices/{endpoint_id}/live`: Read from `cpe_live_state`.
  - `GET /api/v1/devices/{endpoint_id}/history`: Read from `cpe_state_history`.
  - `POST /api/v1/devices/{endpoint_id}/reboot`: Publish TR-369 Reboot Operate message.
- **Reboot Dispatch Implementation Details**:
  - Topic: `usp/endpoint/{endpoint_id}/request`
  - Payload:
    ```python
    record = usp_pb2.Record(
        version="1.3",
        to_id=endpoint_id,
        from_id="proto::controller",
        payload_security=usp_pb2.Record.PLAINTEXT,
        no_session_context=usp_pb2.NoSessionContextRecord(
            payload=usp_pb2.Msg(
                header=usp_pb2.Header(
                    msg_id=str(uuid.uuid4()),
                    msg_type=usp_pb2.Header.OPERATE
                ),
                body=usp_pb2.Body(
                    request=usp_pb2.Request(
                        operate=usp_pb2.Operate(
                            command="Device.Reboot()",
                            command_key=str(uuid.uuid4()),
                            send_resp=True
                        )
                    )
                )
            ).SerializeToString()
        )
    )
    await mqtt_client.publish(f"usp/endpoint/{endpoint_id}/request", record.SerializeToString(), qos=1)
    ```

---

## 6. Caveats
- No session context (`record_type = no_session_context`) is specified for standard event-driven MQTT exchange. While TR-369 also specifies a multi-segment `session_context` (for large payloads exceeding MTP frame sizes), typical MQTT brokers (Mosquitto configured with up to 100MB message limit) easily accommodate single-packet USP messages. Full SAR (Segmentation and Reassembly) can be added as an extension if needed.
- Anonymous MQTT authentication is specified for initial multi-container setup on `acs_network`. For production hardening, TLS 1.3 and mTLS / MQTT username-password authentication should be enabled.

---

## 7. Conclusion
The TR-369 / USP ACS architecture is fully specified and grounded in Broadband Forum standards and project constraints:
1. **MQTT Topics**: Standardized on `usp/endpoint/{endpoint_id}/notify` for inbound telemetry/events and `usp/endpoint/{endpoint_id}/request` for outbound controller commands, matching the wildcard subscription `usp/endpoint/#`.
2. **Protobuf Schema**: Defined a wire-compatible, self-contained `proto/usp.proto` that aligns with BBF TR-369 tag numbers for `Record`, `Msg`, `Operate`, and `Notify`.
3. **Rust USP Core Worker**: Cleanly decoupled using Tokio's MPSC channels, ensuring high-throughput MQTT ingestion remains isolated from database latency and connection pool exhaustion.
4. **FastAPI Command Dispatch**: Provides RESTful APIs and encodes TR-369 `Operate` commands published to Mosquitto.
5. **Database Hybrid Model**: Leverages `UNLOGGED` RAM tables for telemetry ingestion and trigger-based reconciliation for state history.

The specification provides all necessary contracts, schemas, dependencies, and flow definitions for immediate implementation by the engineering agents.

---

## 8. Verification Method
To independently verify the specification once implemented:

1. **Protobuf Compilation Test**:
   - In `rust-core`: run `cargo build` to ensure `prost-build` compiles `proto/usp.proto` without syntax errors.
   - In `python-api`: verify generation of `usp_pb2.py` via `protoc` or `grpcio-tools`.

2. **Integration Verification via CLI (`simulate_flow.sh`)**:
   - Execute the 5-step test script:
     ```bash
     bash simulate_flow.sh
     ```
   - Step 1: Register CPE (`POST /api/v1/devices`). Returns 201.
   - Step 2: Publish TR-369 binary telemetry to Mosquitto (`usp/endpoint/cpe-01/notify`).
   - Step 3: Query live status (`GET /api/v1/devices/cpe-01/live`). Confirm parameter values populated in RAM table.
   - Step 4: Query history (`GET /api/v1/devices/cpe-01/history`). Confirm reconciliation trigger created historical entry.
   - Step 5: Dispatch Reboot command (`POST /api/v1/devices/cpe-01/reboot`). Verify Mosquitto subscriber receives valid USP Operate protobuf payload on `usp/endpoint/cpe-01/request`.
   - Exit code must be `0`.
