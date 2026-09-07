# Handoff Report: Explorer 3 — MPSC Channel Convergence, PostgreSQL Pending Commands & CWMP Session Handling

## 1. Observation

Direct observations from codebase inspection, database DDL, and TR-069 specifications:

### 1.1 Existing Rust Core MPSC Architecture (`rust-core/src/main.rs`)
1. **Channel Creation (`rust-core/src/main.rs:729`)**:
   ```rust
   let (tx, rx) = channel::<TelemetryUpdate>(channel_capacity);
   ```
   `tokio::sync::mpsc::Sender<TelemetryUpdate>` is instantiated with a default capacity of 1024 (configurable via `MPSC_CAPACITY` env var).
2. **Telemetry Data Model (`rust-core/src/main.rs:25-35`)**:
   ```rust
   #[derive(Debug, Clone, Serialize, Deserialize)]
   pub struct TelemetryUpdate {
       pub cpe_id: String,
       pub endpoint_id: Option<String>,
       pub status: String,
       pub current_parameters: JsonValue,
       pub telemetry_metrics: JsonValue,
       pub ip_address: Option<String>,
       pub firmware_version: Option<String>,
       pub timestamp: DateTime<Utc>,
   }
   ```
3. **Database Sink Ingestion (`rust-core/src/main.rs:506-563`)**:
   - Consumes `TelemetryUpdate` from `rx: Receiver<TelemetryUpdate>`.
   - Executes `AUTO_PROVISION_INVENTORY_SQL` (`rust-core/src/main.rs:460-471`):
     ```sql
     INSERT INTO cpe_inventory (
         cpe_id, serial_number, manufacturer, model, status
     ) VALUES (
         $1, $1, 'Auto-Discovered', 'Generic-USP', 'online'
     )
     ON CONFLICT (cpe_id) DO NOTHING;
     ```
     This completely eliminates foreign key constraint violations (`23503`) when un-registered CPEs connect.
   - Executes `UPSERT_LIVE_STATE_SQL` (`rust-core/src/main.rs:473-504`):
     Atomic UPSERT on `cpe_live_state` (located on RAM tmpfs `ram_tablespace`), concatenating `current_parameters` and `telemetry_metrics` with existing JSONB state, updating `last_seen` and `updated_at`.
   - Saturation Backpressure Guard (`rust-core/src/main.rs:634-646`):
     ```rust
     match tokio::time::timeout(Duration::from_millis(500), tx.send(update)).await {
         Ok(Ok(())) => { ... }
         Ok(Err(_)) => { ... }
         Err(_) => {
             tracing::warn!(%topic, "MPSC channel saturated (>500ms); dropping update to preserve MQTT keepalive");
         }
     }
     ```

### 1.2 PostgreSQL Schema & Trigger Invariants (`postgres/init.sql`)
1. **`cpe_pending_commands` DDL (`postgres/init.sql:244-258`)**:
   ```sql
   CREATE TABLE IF NOT EXISTS cpe_pending_commands (
       id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
       cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
       command_type VARCHAR(64) NOT NULL,
       command_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
       status VARCHAR(32) NOT NULL DEFAULT 'pending',
       created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
       dispatched_at TIMESTAMPTZ,
       completed_at TIMESTAMPTZ,
       result_payload JSONB
   );

   CREATE INDEX IF NOT EXISTS idx_cpe_pending_commands_lookup
   ON cpe_pending_commands (cpe_id, status, created_at);
   ```
2. **Reconciliation Trigger (`postgres/init.sql:124-222`)**:
   `reconcile_live_to_history()` fires `AFTER INSERT OR UPDATE ON cpe_live_state`.
   - Reads `rx_optical_power` from `NEW.telemetry_metrics->>'rx_optical_power'`.
   - Compares with `OLD.telemetry_metrics->>'rx_optical_power'`.
   - When `|NEW - OLD| > 1.0 dBm` (or on initial acquisition), inserts into `cpe_historical_metrics` with `change_reason = 'optical_signal_variation'` or `'initial_state'`.
   - Never modifies `cpe_inventory`, eliminating WAL amplification.

### 1.3 FastAPI Command Creation Models (`python-api/app/routers/cpes.py` & `models.py`)
1. **Reboot Endpoint (`python-api/app/routers/cpes.py:216-285`)**:
   Enqueues command into `cpe_pending_commands` when `protocol` is `"tr069"` or `"dual"`:
   - `command_type`: `"Reboot"`
   - `command_payload`: `{"command": "Reboot", "command_key": "reboot-<cpe_id>"}`
   - `status`: `"pending"`
2. **Generic Command Endpoint (`python-api/app/routers/cpes.py:287-315`)**:
   Enqueues commands such as `GetParameterValues`:
   - `command_type`: `"GetParameterValues"`
   - `command_payload`: `{"parameter_names": ["Device.DeviceInfo.SoftwareVersion", "Device.Optical.Interface.1.OpticalSignalLevel"]}`
   - `status`: `"pending"`

---

## 2. Logic Chain

### 2.1 MPSC Channel Convergence Mechanism
1. **Multi-Producer Capabilities**:
   - `tokio::sync::mpsc::Sender<T>` in Rust implements `Clone`. Each cloned instance provides a thread-safe, non-blocking producer handle to the same internal channel buffer.
   - In `rust-core/src/main.rs`, we instantiate `(tx, rx) = channel::<TelemetryUpdate>(channel_capacity)`.
   - We create a clone `let cwmp_tx = tx.clone();` and pass it into the Axum web server's state struct (`AppState`).
   - The MQTT ingest task holds `tx`, and the Axum HTTP router holds `cwmp_tx`. Both concurrently dispatch `TelemetryUpdate` objects into the same queue.
2. **Homogenizing CWMP Inform into `TelemetryUpdate`**:
   Upon receiving an HTTP POST containing a valid `<cwmp:Inform>` SOAP envelope:
   - **`cpe_id`**: Extracted from `<DeviceId><SerialNumber>`. For Huawei EchoLife, this is the physical GPON serial number (e.g., `485754431234ABCD`). For TP-Link, e.g., `22081B12345678`.
   - **`endpoint_id`**: Formatted as `Some(format!("{}-{}-{}", oui, product_class, serial_number))` or fallback `Some(cpe_id.clone())`.
   - **`status`**: Hardcoded to `"online"`.
   - **`current_parameters`**: JSON object containing all key-value pairs extracted from `<ParameterList><ParameterValueStruct>`.
   - **`telemetry_metrics`**: JSON object containing typed metrics:
     - `rx_optical_power`: parsed `f64` from Huawei's `InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower` or TP-Link's `Device.Optical.Interface.1.OpticalSignalLevel`.
     - Additional metrics (TX power, CPU, temperature) if present in the data model.
   - **`ip_address`**: Extracted from incoming socket connection (`axum::extract::ConnectInfo<SocketAddr>`) or from `ConnectionRequestURL`.
   - **`firmware_version`**: Extracted from `SoftwareVersion` parameter.
   - **`timestamp`**: `Utc::now()`.
3. **Seamless Ingestion by Existing `run_db_sink`**:
   - Because `TelemetryUpdate` is protocol-agnostic, `run_db_sink` consumes the update identically to MQTT-originated updates.
   - `AUTO_PROVISION_INVENTORY_SQL` automatically provisions the device in `cpe_inventory` if unseen before.
   - `UPSERT_LIVE_STATE_SQL` commits the parameters and metrics to `cpe_live_state` in the RAM tablespace (`tmpfs`).
   - The PostgreSQL trigger `reconcile_live_to_history()` inspects `NEW.telemetry_metrics->>'rx_optical_power'` and commits a historical snapshot when `|delta| > 1.0 dBm`.
   - **Result**: Zero modifications are required to `run_db_sink` or the PostgreSQL database schema.

### 2.2 Pending Commands Retrieval & Status Transition
1. **Concurrency Control via `FOR UPDATE SKIP LOCKED`**:
   In high-throughput environments where multiple HTTP workers or rapid periodic Informs occur, two requests could attempt to process pending commands simultaneously.
   - Query:
     ```sql
     SELECT id, command_type, command_payload
     FROM cpe_pending_commands
     WHERE cpe_id = $1 AND status = 'pending'
     ORDER BY created_at ASC
     LIMIT 1
     FOR UPDATE SKIP LOCKED;
     ```
   - Rationale:
     - `ORDER BY created_at ASC LIMIT 1`: Enforces strict FIFO command execution.
     - `FOR UPDATE`: Row-level exclusive lock prevents any other transaction from selecting or updating this command.
     - `SKIP LOCKED`: Ensures non-blocking execution; if a row is already locked by a concurrent transaction, it is skipped rather than stalled.
2. **Transaction Scope**:
   In PostgreSQL, row locks acquired by `FOR UPDATE` are released immediately upon transaction completion. If executed in auto-commit mode, the lock is released before the update can occur.
   - Implementation:
     ```rust
     let mut tx = pool.begin().await?;

     let cmd = sqlx::query_as::<_, PendingCommandRow>(
         r#"
         SELECT id, command_type, command_payload
         FROM cpe_pending_commands
         WHERE cpe_id = $1 AND status = 'pending'
         ORDER BY created_at ASC
         LIMIT 1
         FOR UPDATE SKIP LOCKED
         "#
     )
     .bind(&cpe_id)
     .fetch_optional(&mut *tx)
     .await?;

     if let Some(ref command) = cmd {
         sqlx::query(
             r#"
             UPDATE cpe_pending_commands
             SET status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP
             WHERE id = $1
             "#
         )
         .bind(command.id)
         .execute(&mut *tx)
         .await?;
     }

     tx.commit().await?;
     ```
   - Transition Guarantee: The command atomically shifts from `'pending'` to `'dispatched'`.

3. **Type Handling for `id`**:
   - In `init.sql`, `id` is `UUID`.
   - If the `sqlx` crate dependency in `Cargo.toml` has `features = ["uuid"]` and `uuid = { version = "1.0", features = ["serde", "v4"] }`, the struct uses `pub id: uuid::Uuid`.
   - Alternatively, if avoiding the `uuid` crate dependency, the SQL query can cast `id::text AS id` and update `WHERE id = $1::uuid`. Using `uuid::Uuid` directly is recommended for maximum type safety.

### 2.3 CWMP Session Handling & Empty POST Lifecycle
TR-069 specifies a strictly synchronized half-duplex HTTP request-response flow:

```
ONT / CPE (HTTP Client)                                     ACS / Rust Core (HTTP Server: 7547)
       |                                                                   |
       |--- 1. POST / (cwmp:Inform XML) ---------------------------------->|
       |                                                                   | [1. Parse XML Inform]
       |                                                                   | [2. Send TelemetryUpdate to MPSC]
       |                                                                   | [3. Extract cwmp:ID header]
       |                                                                   | [4. Cache session: cpe_id <-> IP]
       |                                                                   |
       |<-- 2. HTTP 200 OK (cwmp:InformResponse) --------------------------|
       |       Header: Set-Cookie: session=<cpe_id>; Path=/; HttpOnly      |
       |       Body: <cwmp:InformResponse><MaxEnvelopes>1...               |
       |                                                                   |
       |--- 3. POST / (Empty Body, Content-Length: 0) -------------------->|
       |       Header: Cookie: session=<cpe_id>                            |
       |       (ONT signals: "I have no more requests; your turn ACS")     |
       |                                                                   | [1. Extract cpe_id from Cookie/IP]
       |                                                                   | [2. Query cpe_pending_commands]
       |                                                                   | [3. Found: Reboot or GPV]
       |                                                                   | [4. Update status = 'dispatched']
       |                                                                   |
       |<-- 4. HTTP 200 OK (SOAP RPC: cwmp:Reboot) ------------------------|
       |       Body: <SOAP-ENV:Envelope>...<cwmp:Reboot>...                |
       |                                                                   |
       | [ONT executes RPC]                                                |
       |--- 5. POST / (cwmp:RebootResponse XML) --------------------------->|
       |       Header: Cookie: session=<cpe_id>                            |
       |       Body: <cwmp:RebootResponse/>                                |
       |                                                                   | [1. Update status = 'completed']
       |                                                                   | [2. Check next pending command]
       |                                                                   | [3. Queue is empty]
       |                                                                   |
       |<-- 6. HTTP 200 OK (Empty Body, Content-Length: 0) ----------------|
       |       (ACS signals: "I have no more requests; session ended")     |
       |                                                                   |
       |=== [ONT closes TCP connection] ===================================|
```

1. **Phase 1: Inform & InformResponse**:
   - ONT initiates session with `POST /` containing `<cwmp:Inform>`.
   - ACS parses metadata, extracts header ID (e.g., `<cwmp:ID>10001</cwmp:ID>`).
   - Dispatches `TelemetryUpdate` to MPSC channel.
   - Generates `<cwmp:InformResponse>` echoing the header ID.
   - Injects HTTP response header: `Set-Cookie: session={cpe_id}; Path=/; HttpOnly`.
2. **Phase 2: Empty POST (ACS Turn)**:
   - ONT acknowledges `InformResponse` by sending an **Empty POST** (`Content-Length: 0` or empty payload).
   - Axum handler inspects request body: `if body.is_empty()`.
   - Identifies the CPE via `Cookie: session=<cpe_id>`.
   - Queries `cpe_pending_commands` using `FOR UPDATE SKIP LOCKED`.
   - **Branch A (Command Pending)**:
     - Dispatches SOAP XML RPC (`cwmp:Reboot`, `cwmp:GetParameterValues`).
     - Returns HTTP 200 OK with `Content-Type: text/xml; charset=utf-8`.
   - **Branch B (Queue Empty)**:
     - Returns **Empty HTTP 200 OK** (`Content-Length: 0`).
     - Signals session completion per TR-069 Section 3.7.1. ONT closes TCP socket.
3. **Phase 3: Handling Command Responses from ONT**:
   - If a command was dispatched, the ONT returns:
     - `<cwmp:RebootResponse/>`: Success confirmation. ACS marks status `'completed'`.
     - `<cwmp:GetParameterValuesResponse>`: Contains parameter key-values. ACS marks status `'completed'`, stores result JSONB in `result_payload`, and enqueues newly acquired parameters into MPSC to update `cpe_live_state`.
     - `<SOAP-ENV:Fault>`: Command failed (e.g. invalid parameter). ACS marks status `'failed'` and records fault details in `result_payload`.
   - ACS then checks for the next pending command. If none remain, replies with Empty HTTP 200 OK.

4. **Dual Session Tracking Mechanism**:
   - **Primary**: RFC 6265 HTTP Cookie (`Set-Cookie: session=<cpe_id>; Path=/; HttpOnly`). Compliant with TR-069 Amendment 5 Section 3.2.1.1.
   - **Fallback**: In-memory IP session cache (`Arc<RwLock<HashMap<IpAddr, (String, Instant)>>>`).
     - If an ONT stripped or omitted the Cookie header on the Empty POST, the handler looks up the sender's `IpAddr` in the session cache.
     - TTL: 60 seconds (covers typical CWMP session duration of 2–10 seconds).
     - Provides complete fault tolerance against non-compliant or legacy ONT firmware.

---

## 3. Caveats

1. **Transaction Lifetime with Long-Polling**:
   The `SELECT ... FOR UPDATE SKIP LOCKED` query and the `UPDATE ... SET status = 'dispatched'` query must be executed in the same database transaction immediately during request processing. Do NOT keep the SQL transaction open across HTTP network round-trips with the ONT.
2. **Unacknowledged Dispatched Commands**:
   If an ONT abruptly reboots before sending `cwmp:RebootResponse` or drops the TCP connection, the command remains in `status = 'dispatched'`. A background reaper task or subsequent Inform session can inspect commands stuck in `'dispatched'` for > 5 minutes and mark them `'failed'` or reset them to `'pending'`.
3. **Channel Backpressure Isolation**:
   If the PostgreSQL sink task slows down and the MPSC channel fills to capacity (1024), `tx.send(update)` must NOT block indefinitely. A timeout of 500ms (`tokio::time::timeout(Duration::from_millis(500), tx.send(update))`) must be used. If the channel is saturated, the telemetry update is dropped with a logged warning, but the HTTP InformResponse is still delivered to the ONT. This prevents ONT retry storms and connection timeouts.
4. **UUID Feature in `rust-core/Cargo.toml`**:
   `rust-core/Cargo.toml` currently defines `sqlx = { version = "0.7", features = ["postgres", "json", "chrono"] }`. Adding `"uuid"` to `sqlx` features or casting `id::text` in SQL queries is necessary to bind the `cpe_pending_commands.id` column properly.

---

## 4. Conclusion

1. **MPSC Convergence**:
   Cloning `tx: tokio::sync::mpsc::Sender<TelemetryUpdate>` and passing it to the Axum state allows TR-069 Inform messages to be transformed into `TelemetryUpdate` and fed into `run_db_sink` with **zero schema changes** and **zero downstream logic modifications**.
2. **Atomic Queue Delivery**:
   Using `SELECT id, command_type, command_payload FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED` inside a short-lived PostgreSQL transaction guarantees thread-safe, strictly ordered FIFO delivery without lock contention.
3. **Robust CWMP Session Cycle**:
   The half-duplex CWMP protocol is fully managed by handling both Inform and Empty POST (`Content-Length: 0`) in the Axum route handler, using cookie-based session tracking with an in-memory IP cache fallback.

---

## 5. Architectural Implementation Blueprint

### 5.1 Axum AppState Definition
```rust
#[derive(Clone)]
pub struct AppState {
    pub tx: Sender<TelemetryUpdate>,
    pub db_pool: PgPool,
    pub session_cache: Arc<tokio::sync::RwLock<std::collections::HashMap<std::net::IpAddr, (String, std::time::Instant)>>>,
}
```

### 5.2 Pending Command Query & Transition Function
```rust
#[derive(Debug, sqlx::FromRow)]
pub struct PendingCommandRow {
    pub id: uuid::Uuid, // Or String if casting id::text
    pub command_type: String,
    pub command_payload: serde_json::Value,
}

pub async fn dequeue_pending_command(
    pool: &PgPool,
    cpe_id: &str,
) -> Result<Option<PendingCommandRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;

    let query = r#"
        SELECT id, command_type, command_payload
        FROM cpe_pending_commands
        WHERE cpe_id = $1 AND status = 'pending'
        ORDER BY created_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    "#;

    let cmd: Option<PendingCommandRow> = sqlx::query_as(query)
        .bind(cpe_id)
        .fetch_optional(&mut *tx)
        .await?;

    if let Some(ref command) = cmd {
        sqlx::query(
            r#"
            UPDATE cpe_pending_commands
            SET status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP
            WHERE id = $1
            "#
        )
        .bind(command.id)
        .execute(&mut *tx)
        .await?;
    }

    tx.commit().await?;
    Ok(cmd)
}

pub async fn mark_command_completed(
    pool: &PgPool,
    command_id: uuid::Uuid,
    result_payload: serde_json::Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        UPDATE cpe_pending_commands
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP, result_payload = $2
        WHERE id = $1
        "#
    )
    .bind(command_id)
    .bind(result_payload)
    .execute(pool)
    .await?;
    Ok(())
}

pub async fn mark_command_failed(
    pool: &PgPool,
    command_id: uuid::Uuid,
    fault_payload: serde_json::Value,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        r#"
        UPDATE cpe_pending_commands
        SET status = 'failed', completed_at = CURRENT_TIMESTAMP, result_payload = $2
        WHERE id = $1
        "#
    )
    .bind(command_id)
    .bind(fault_payload)
    .execute(pool)
    .await?;
    Ok(())
}
```

### 5.3 SOAP RPC XML Template Generators
```rust
pub fn build_inform_response_xml(header_id: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:InformResponse>
      <MaxEnvelopes>1</MaxEnvelopes>
    </cwmp:InformResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        header_id
    )
}

pub fn build_reboot_rpc_xml(cmd_id: &str, command_key: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Reboot>
      <CommandKey>{}</CommandKey>
    </cwmp:Reboot>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        cmd_id, command_key
    )
}

pub fn build_get_parameter_values_rpc_xml(cmd_id: &str, parameter_names: &[String]) -> String {
    let mut param_elements = String::new();
    for name in parameter_names {
        param_elements.push_str(&format!("        <string>{}</string>\n", name));
    }

    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:GetParameterValues>
      <ParameterNames soap-enc:arrayType="xsd:string[{}]">
{}      </ParameterNames>
    </cwmp:GetParameterValues>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        cmd_id,
        parameter_names.len(),
        param_elements
    )
}
```

---

## 6. Verification Method

### Step 1: Send CWMP Inform Request
Simulate Huawei EchoLife Inform via curl:
```bash
curl -i -X POST http://localhost:7547/ \
  -H "Content-Type: text/xml; charset=utf-8" \
  -H "SOAPAction: \"\"" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">10001</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd.</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>485754431234ABCD</SerialNumber>
      </DeviceId>
      <Event soap-enc:arrayType="cwmp:EventStruct[1]">
        <EventStruct>
          <EventCode>2 PERIODIC</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
      <MaxEnvelopes>1</MaxEnvelopes>
      <CurrentTime>2026-09-07T14:30:00Z</CurrentTime>
      <RetryCount>0</RetryCount>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[2]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value>V5R019C00S105</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value>-19.50</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>'
```
**Verification**:
- HTTP Response: `200 OK`.
- Header: `Set-Cookie: session=485754431234ABCD; Path=/; HttpOnly`.
- Body: Contains `<cwmp:InformResponse>` with `<cwmp:ID>10001</cwmp:ID>`.

### Step 2: Verify Database Ingestion (MPSC & Reconcile Trigger)
Check that `cpe_live_state` and `cpe_historical_metrics` were populated:
```bash
docker exec acs_postgres psql -U acs_user -d acs_db -c \
  "SELECT cpe_id, status, telemetry_metrics->>'rx_optical_power' AS rx_power, last_seen FROM cpe_live_state WHERE cpe_id = '485754431234ABCD';"

docker exec acs_postgres psql -U acs_user -d acs_db -c \
  "SELECT cpe_id, optical_power, change_reason FROM cpe_historical_metrics WHERE cpe_id = '485754431234ABCD';"
```

### Step 3: Queue a Pending Command via FastAPI
Queue a reboot command for this ONT:
```bash
curl -i -X POST http://localhost:8000/api/v1/cpes/485754431234ABCD/commands \
  -H "Content-Type: application/json" \
  -d '{"command_type": "Reboot", "command_payload": {"command": "Reboot", "command_key": "reboot-cmd-999"}}'
```
**Verification**: Command status is `"pending"`.

### Step 4: Send Empty HTTP POST (Simulate ONT Request Phase)
Send an empty POST with the session cookie:
```bash
curl -i -X POST http://localhost:7547/ \
  -H "Cookie: session=485754431234ABCD" \
  -H "Content-Length: 0"
```
**Verification**:
- HTTP Response: `200 OK`.
- Body: Contains `<cwmp:Reboot>` SOAP envelope with `<CommandKey>reboot-cmd-999</CommandKey>`.
- In database, `cpe_pending_commands` status has shifted to `'dispatched'` with `dispatched_at` timestamp.

### Step 5: Send Empty HTTP POST when Queue is Empty
Send an empty POST again:
```bash
curl -i -X POST http://localhost:7547/ \
  -H "Cookie: session=485754431234ABCD" \
  -H "Content-Length: 0"
```
**Verification**:
- HTTP Response: `200 OK`.
- `Content-Length: 0` (empty body), completing the session cleanly.
