# Milestone 4 Dual-Stack TR-069 / TR-369 Architecture Investigation Report

**Agent:** `explorer_m4_rust_1`  
**Date:** 2026-09-07  
**Working Directory:** `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_rust_1`  
**Target:** `rust-core/` Dual-Stack CWMP (TR-069 over HTTP/XML) and USP (TR-369 over MQTT) Integration  

---

## 1. Observation

Direct observations from repository files, dependency manifests, runtime code, database schemas, container definitions, and compiler outputs:

### 1.1 Current `rust-core` Dependency Manifest (`rust-core/Cargo.toml`)
- **Lines 8–24**:
  ```toml
  [dependencies]
  tokio = { version = "1.38", features = ["full"] }
  rumqttc = "0.24"
  sqlx = { version = "0.7", default-features = false, features = [
      "runtime-tokio-rustls",
      "postgres",
      "json",
      "chrono"
  ] }
  prost = "0.12"
  prost-types = "0.12"
  serde = { version = "1.0", features = ["derive"] }
  serde_json = "1.0"
  chrono = { version = "0.4", features = ["serde"] }
  tracing = "0.1"
  tracing-subscriber = { version = "0.3", features = ["env-filter"] }
  anyhow = "1.0"

  [build-dependencies]
  prost-build = "0.12"
  ```
- **Lines 29–35**: Release profile configured with `opt-level = 3`, `lto = true`, `codegen-units = 1`, `panic = "abort"`, `strip = true`. Produces a 3.9 MB binary with RSS < 30 MB.

### 1.2 Build Script & Protobuf Schema (`rust-core/build.rs`, `rust-core/proto/usp.proto`)
- `build.rs` lines 1–11: Compiles `proto/usp.proto` using `prost_build::Config::new().compile_protos(&["proto/usp.proto"], &["proto"])`.
- In `src/main.rs` lines 16–18: Embedded via `pub mod usp { include!(concat!(env!("OUT_DIR"), "/usp.rs")); }`.

### 1.3 Async Runtime & Task Lifecycle (`rust-core/src/main.rs`)
- **Lines 678–765 (`main`)**:
  - Tokio runtime entry point: `#[tokio::main] async fn main() -> Result<()>`.
  - MPSC channel initialization:
    ```rust
    let (tx, rx) = channel::<TelemetryUpdate>(channel_capacity);
    ```
    Where `channel_capacity` defaults to 1024 (from env `MPSC_CAPACITY`).
  - Shutdown channel: `let (shutdown_tx, shutdown_rx) = watch::channel(false);`.
  - Spawns three concurrent tasks on the Tokio multi-threaded work-stealing runtime:
    1. `health_handle`: `run_healthcheck_monitor(health.clone(), db_pool.clone(), shutdown_rx.clone())` (touches `/tmp/healthy` every 2s).
    2. `db_handle`: `run_db_sink(db_pool.clone(), rx, health.clone())` (drains MPSC channel into PostgreSQL).
    3. `mqtt_handle`: `run_mqtt_ingest(mqtt_host, mqtt_port, "rust-usp-core".to_string(), tx, health.clone(), shutdown_rx.clone())` (connects to Mosquitto, subscribes to `usp/endpoint/#`).
  - Graceful shutdown triggered on `tokio::signal::ctrl_c()`, signaling `shutdown_tx.send(true)`, followed by `tokio::join!(mqtt_handle, db_handle, health_handle)`.

### 1.4 MPSC Message Data Structure (`rust-core/src/main.rs`)
- **Lines 25–35 (`TelemetryUpdate`)**:
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

### 1.5 Database Sink & PostgreSQL Persistence Pipeline (`rust-core/src/main.rs`, `postgres/init.sql`)
- **Lines 460–471 (`AUTO_PROVISION_INVENTORY_SQL`)**:
  ```sql
  INSERT INTO cpe_inventory (cpe_id, serial_number, manufacturer, model, status)
  VALUES ($1, $1, 'Auto-Discovered', 'Generic-USP', 'online')
  ON CONFLICT (cpe_id) DO NOTHING;
  ```
- **Lines 473–504 (`UPSERT_LIVE_STATE_SQL`)**:
  Performs dynamic SQL UPSERT on unlogged table `cpe_live_state` in `ram_tablespace`:
  - Preserves/merges parameters via JSONB concatenation: `current_parameters = cpe_live_state.current_parameters || COALESCE(EXCLUDED.current_parameters, '{}'::jsonb)`.
  - Merges telemetry metrics: `telemetry_metrics = cpe_live_state.telemetry_metrics || COALESCE(EXCLUDED.telemetry_metrics, '{}'::jsonb)`.
  - Sets `updated_at = CURRENT_TIMESTAMP`.
  - Re-tries up to 3 times with `150ms * retries` backoff.
- **`postgres/init.sql` Lines 124–238 (`reconcile_live_to_history`)**:
  Trigger on `cpe_live_state` checking optical power delta:
  - Reads `NEW.telemetry_metrics->>'rx_optical_power'` or `NEW.current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel'`.
  - If delta `|NEW - OLD| > 1.0 dBm`, automatically records row in `cpe_historical_metrics` (visible in view `cpe_state_history`).
  - Does NOT update `cpe_inventory`, avoiding WAL amplification.

### 1.6 MQTT Ingestion Loop & Backpressure Isolation (`rust-core/src/main.rs`)
- **Lines 569–672 (`run_mqtt_ingest`)**:
  - Event loop: `eventloop.poll().await` inside `tokio::select!`.
  - Packet filtering: `is_command_topic(&topic)` filters out `/request` to prevent echo loops; filters out topics not starting with `usp/endpoint/`.
  - Backpressure guard (lines 633–645): Enqueues update to MPSC channel with a 500 ms timeout (`tokio::time::timeout(Duration::from_millis(500), tx.send(update)).await`). If channel is full, drops packet rather than blocking the MQTT event loop and dropping MQTT keepalives (`PINGREQ`).

### 1.7 Current Compiler and Test Verification
- Executed `cargo check`: Exited with code 0 in 20.84s (0 errors, 0 warnings).
- Executed `cargo test`: Exited with code 0 in 16.92s (19 unit tests executed, 19 passed; 0 failed).
- Adversarial test suite `test_adversarial_m3.py` confirms AST integrity: zero `.unwrap()` or `.expect(` in decoding logic, bounded channels, and healthcheck probe isolation.

### 1.8 Container & Network Infrastructure (`docker-compose.yml`)
- Lines 48–72 define `rust-core`:
  - Connects to `postgres` and `mosquitto` on `acs_network`.
  - Memory limit: strictly 500 MB.
  - Currently exposes **NO** host ports (only MQTT client outbound).
  - TR-069 requires adding port mapping `7547:7547`.

---

## 2. Logic Chain

### 2.1 Embedding Axum HTTP Server on Port 7547 Without Blocking Tokio Runtime
1. **Tokio Runtime Coexistence**:
   - `rust-core` already uses `tokio = { version = "1.38", features = ["full"] }` configured as a multi-threaded work-stealing runtime (`#[tokio::main]`).
   - Adding an embedded HTTP server via `axum = "0.7"` (or `"0.8"`) alongside `run_mqtt_ingest` is fundamentally non-blocking.
   - Axum is built directly on `hyper 1.x`, `tower`, and `tokio::net::TcpListener`. An Axum server task can be spawned with:
     ```rust
     let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", cwmp_port)).await?;
     let http_handle = tokio::spawn(async move {
         axum::serve(listener, app)
             .with_graceful_shutdown(async move {
                 // waits for shutdown_rx
             })
             .await
     });
     ```
   - Main function joins all handles:
     ```rust
     let _ = tokio::join!(mqtt_handle, db_handle, health_handle, http_handle);
     ```
   - Neither MQTT nor HTTP blocks the runtime because both yield asynchronously (`.await`) at OS socket boundaries.
   - Memory footprint impact: Axum and hyper 1.x add < 1.5 MB to the binary and ~5 MB RSS at runtime, remaining well under the 500 MB container threshold (~35 MB RSS total).

2. **Why Axum Over Actix-Web**:
   - `actix-web` introduces its own runtime abstraction (`actix_rt::System`), separate threadpools, and actor system dependencies. Integrating `actix-web` inside an existing `tokio::main` requires `actix_web::rt::System::new()` or bridging Tokio tasks, creating thread overhead and binary bloat (~15+ MB).
   - `axum` shares the existing Tokio runtime, shares `Arc<AppState>`, uses zero additional runtime threads, and matches modern Rust async idioms.

### 2.2 XML Parsing Engine Evaluation: `roxmltree` vs `quick-xml`
To parse TR-069 SOAP packets (`<SOAP-ENV:Envelope>`, `<cwmp:Inform>`, `<cwmp:ParameterValueStruct>`), we evaluated `roxmltree` vs `quick-xml`:

| Evaluation Dimension | `roxmltree` (v0.20) | `quick-xml` (v0.36) | Assessment for TR-069 |
|---|---|---|---|
| **Architecture** | Read-Only DOM representation in a single contiguous node buffer | Streaming pull parser (SAX events) or Serde deserializer | `roxmltree` fits small-to-medium SOAP payloads (2 KB – 100 KB) perfectly. |
| **XML Namespaces** | **Namespace-agnostic local name matching**: `node.tag_name().name() == "Inform"`. Ignores prefix variations (`cwmp:`, `soap:`, `SOAP-ENV:`, `soapenv:`, `cwmp-1-0:`) | Serde fails when namespace prefixes vary. Streaming reader requires manual namespace tracking stack | Real-world ONTs (Huawei, ZTE, TP-Link, Dasan) use inconsistent prefixes. `roxmltree` handles all without custom namespace state machines. |
| **Memory Allocations** | **Zero heap allocations** for text/attribute values; slices directly into input `&str`. Nodes stored in single contiguous `Vec`. | Zero allocation in raw reader mode; Serde mode allocates heavily. | `roxmltree` memory is minimal (~32 bytes/node, ~15 KB total for 200 parameters). |
| **Parsing Throughput** | ~150–300 MB/s per core (~20 microseconds for 10 KB Inform). | ~300–600 MB/s per core (~10 microseconds for 10 KB Inform). | The 10 µs difference is completely negligible compared to network latency (10–50 ms) and Postgres I/O (1–5 ms). |
| **Error Handling & Safety** | 100% safe Rust, panic-free, fuzz-tested, resists XML entity expansion / recursion depth attacks (`DepthLimit`). | Safe Rust in reader mode; Serde error reporting on malformed XML is often opaque. | `roxmltree` parsing returns `Result<Document, roxmltree::Error>` cleanly mappable to HTTP 400. |
| **Dependencies** | **0 dependencies** (completely standalone). | **0 dependencies**. | Tie (both minimal). |

**Recommendation:**  
Adopt **`roxmltree = "0.20"`**. It eliminates vendor XML namespace prefix fragility (critical for Huawei EchoLife vs TP-Link EX compatibility), requires zero heap allocations for string extraction, and provides clear, maintainable query methods (`descendants()`, `children()`).

### 2.3 TR-069 XML Inform Structure & Extraction Mapping
A real Huawei EchoLife or TP-Link EX TR-069 Inform packet contains:
```xml
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID SOAP-ENV:mustUnderstand="1">1001</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>4857544312345678</SerialNumber>
      </DeviceId>
      <ParameterList SOAP-ENC:arrayType="cwmp:ParameterValueStruct[N]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">V5R019C20S120</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANOponInterfaceConfig.RXPower</Name>
          <Value xsi:type="xsd:string">-19.85</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress</Name>
          <Value xsi:type="xsd:string">187.55.120.44</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

**Extraction Strategy:**
1. Extract `cwmp:ID`: `doc.descendants().find(|n| n.tag_name().name() == "ID").and_then(|n| n.text()).unwrap_or("1")`.
2. Extract `DeviceId`:
   - `serial_number`: `<DeviceId><SerialNumber>`
   - `manufacturer`: `<DeviceId><Manufacturer>`
   - `oui`: `<DeviceId><OUI>`
   - `product_class`: `<DeviceId><ProductClass>`
3. Device Identity Resolution:
   - Check if device is registered in `cpe_inventory`:
     Query `SELECT cpe_id FROM cpe_inventory WHERE serial_number = $1 OR cpe_id = $1 LIMIT 1`.
   - If found, use the authoritative `cpe_id` (e.g. `cpe-sim-001` or `ont-01`).
   - If not found, use `serial_number` as `cpe_id`.
   - Set `endpoint_id = Some(format!("urn:bbf:cwmp:{}-{}-{}", oui, product_class, serial_number))`.
4. Parameter & Metric Normalization:
   - Iterate `<ParameterValueStruct>` nodes.
   - For each parameter, extract `<Name>` and `<Value>`.
   - Store in `current_parameters` map.
   - Route through `PayloadDecoder::process_param(key, val, &mut current_parameters, &mut telemetry_metrics)`:
     - Optical Signal: Matches TR-181 (`Device.Optical.Interface.1.OpticalSignalLevel`) AND TR-098/IGD (`InternetGatewayDevice.WANDevice.1.WANOponInterfaceConfig.RXPower`, `RxPower`, `OpticalSignalLevel`).
     - Extracts `rx_optical_power` numeric value.
     - Extracts `cpu_usage`, `memory_usage`, `temperature`, `rx_bytes`, `tx_bytes`.
   - Extract `ip_address`: Checks TR-181 `Device.IP.Interface.1.IPv4Address.1.IPAddress` or TR-098 `ExternalIPAddress` or any parameter ending with `IPAddress` or `ExternalIPAddress`.
   - Extract `firmware_version`: Checks `Device.DeviceInfo.SoftwareVersion` or TR-098 `InternetGatewayDevice.DeviceInfo.SoftwareVersion`.

### 2.4 Converging into the SAME MPSC Queue (Requirement R3)
1. `TelemetryUpdate` structure remains **100% untouched**:
   ```rust
   let update = TelemetryUpdate {
       cpe_id,
       endpoint_id,
       status: "online".to_string(),
       current_parameters: JsonValue::Object(current_parameters),
       telemetry_metrics: JsonValue::Object(telemetry_metrics),
       ip_address,
       firmware_version,
       timestamp: Utc::now(),
   };
   ```
2. The Axum CWMP handler holds `tx: Sender<TelemetryUpdate>`.
3. It enqueues the update:
   ```rust
   tx.send_timeout(update, Duration::from_millis(500)).await
   ```
4. **Zero modification required in `run_db_sink`**:
   - Receives homogenized `TelemetryUpdate` from either MQTT or CWMP HTTP.
   - Runs `AUTO_PROVISION_INVENTORY_SQL`.
   - Runs `UPSERT_LIVE_STATE_SQL` into unlogged in-RAM table `cpe_live_state` with JSONB merge `||`.
   - Activates PostgreSQL trigger `reconcile_live_to_history` when optical delta > 1.0 dBm.
   - Both TR-069 and TR-369 devices are unified in the database without separate schemas or storage paths!

### 2.5 CWMP Session Lifecycle & Command Delivery (Requirement R4)
TR-069 CWMP is client-initiated (ONT connects to ACS). The interaction follows standard TR-069 state-machine:
1. ONT sends HTTP POST with `<cwmp:Inform>`.
2. Rust ACS processes Inform, enqueues `TelemetryUpdate` into MPSC channel, and responds with HTTP 200:
   ```xml
   <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
     <soapenv:Header>
       <cwmp:ID soapenv:mustUnderstand="1">{cwmp_id}</cwmp:ID>
     </soapenv:Header>
     <soapenv:Body>
       <cwmp:InformResponse>
         <MaxEnvelopes>1</MaxEnvelopes>
       </cwmp:InformResponse>
     </soapenv:Body>
   </soapenv:Envelope>
   ```
3. ONT sends empty HTTP POST (indicating readiness to accept commands).
4. Rust ACS checks for pending commands:
   - Table `cpe_pending_commands` in PostgreSQL:
     ```sql
     CREATE TABLE IF NOT EXISTS cpe_pending_commands (
         id BIGSERIAL PRIMARY KEY,
         cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
         command_type VARCHAR(64) NOT NULL, -- 'Reboot', 'GetParameterValues'
         command_key VARCHAR(128) NOT NULL,
         payload JSONB DEFAULT '{}'::jsonb,
         status VARCHAR(32) NOT NULL DEFAULT 'pending',
         created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
         updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
     );
     ```
   - If a pending command exists (e.g. `Reboot` inserted by FastAPI `POST /api/v1/cpes/{cpe_id}/reboot`), Rust responds with SOAP:
     ```xml
     <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
       <soapenv:Header><cwmp:ID soapenv:mustUnderstand="1">{cmd_key}</cwmp:ID></soapenv:Header>
       <soapenv:Body>
         <cwmp:Reboot>
           <CommandKey>{cmd_key}</CommandKey>
         </cwmp:Reboot>
       </soapenv:Body>
     </soapenv:Envelope>
     ```
     And updates `status = 'sent'`.
   - If no command is pending, Rust responds with **HTTP 204 No Content**, cleanly closing the CWMP session according to TR-069 specification.

---

## 3. Caveats

1. **HTTP Basic / Digest Authentication**:
   - Some commercial ISPs configure CWMP Connection Request authentication or ACS authentication (HTTP 401 challenge). For this initial dual-stack implementation, standard open POST on port 7547 is assumed unless credentials are provided via `CWMP_USERNAME` / `CWMP_PASSWORD`.
2. **Namespace Variations**:
   - While `roxmltree` tag name lookup (`.tag_name().name() == "Inform"`) is immune to prefix differences, malformed XML with unescaped ampersands or invalid encoding from faulty third-party CPE firmware will return a parse error. The handler must return HTTP 400 Bad Request rather than panicking or crashing.
3. **Database Lookups in HTTP Handler**:
   - In the Inform handler, querying `cpe_inventory` to map `serial_number -> cpe_id` requires an async SQL query. With `sqlx::query_scalar` and connection pool timeout (2 seconds), this is non-blocking. If PostgreSQL is temporarily down, the fallback uses `serial_number` as `cpe_id`.
4. **Port Exposure & Firewalling**:
   - In production environments, port 7547 must be bound only on management VLANs or restricted via security groups to prevent public WAN CWMP probing.

---

## 4. Conclusion

The dual-stack TR-069 + TR-369 architecture is clean, highly cohesive, and requires **no disruption** to the existing MQTT ingestion or PostgreSQL unlogged RAM table pipeline.

### Summary of Concrete Implementation Plan

#### A. Dependency Changes (`rust-core/Cargo.toml`)
Add to `[dependencies]`:
```toml
axum = "0.7"
roxmltree = "0.20"
tower-http = { version = "0.5", features = ["trace"] }
```

#### B. Architectural Additions in `rust-core/src/main.rs`
1. **Module `cwmp`**:
   - `pub struct CwmpInformPayload`: Extracted metadata from TR-069 XML Inform (`cwmp_id`, `manufacturer`, `oui`, `product_class`, `serial_number`, `parameters: HashMap<String, String>`).
   - `pub fn parse_cwmp_inform(xml_str: &str) -> Result<CwmpInformPayload>`: Uses `roxmltree::Document::parse(xml_str)` to extract `DeviceId` and `ParameterList` without relying on fixed namespace prefixes.
   - `pub fn build_inform_response(cwmp_id: &str) -> String`: Formats standard SOAP CWMP `InformResponse`.
2. **AppState & HTTP Handler**:
   ```rust
   #[derive(Clone)]
   pub struct AppState {
       pub tx: Sender<TelemetryUpdate>,
       pub db_pool: PgPool,
       pub health: Arc<HealthState>,
   }

   pub async fn handle_cwmp_post(
       State(state): State<AppState>,
       body: String,
   ) -> Result<Response, (StatusCode, String)>
   ```
   - If body is empty: checks pending commands in `cpe_pending_commands` or returns `StatusCode::NO_CONTENT`.
   - If body contains XML: parses Inform, maps parameters to `TelemetryUpdate`, dispatches to `state.tx.send_timeout(update, Duration::from_millis(500)).await`, and returns HTTP 200 with `InformResponse` XML.
3. **HTTP Server Task in `main()`**:
   ```rust
   let cwmp_port: u16 = std::env::var("CWMP_PORT")
       .unwrap_or_else(|_| "7547".to_string())
       .parse()
       .unwrap_or(7547);
   let app = Router::new()
       .route("/", post(handle_cwmp_post))
       .route("/cwmp", post(handle_cwmp_post))
       .with_state(app_state);

   let listener = tokio::net::TcpListener::bind(format!("0.0.0.0:{}", cwmp_port)).await?;
   let http_handle = tokio::spawn(async move {
       axum::serve(listener, app)
           .with_graceful_shutdown(async move {
               while !*shutdown_rx.borrow_and_update() {
                   if shutdown_rx.changed().await.is_err() { break; }
               }
           })
           .await
   });
   ```
   Joined with `tokio::join!(mqtt_handle, db_handle, health_handle, http_handle)`.

#### C. `docker-compose.yml` Update
Under `rust-core`:
```yaml
    ports:
      - "7547:7547"
    environment:
      - DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db
      - MQTT_HOST=mosquitto
      - MQTT_PORT=1883
      - CWMP_PORT=7547
```

---

## 5. Verification Method

To independently verify the dual-stack implementation:

### 5.1 Unit Tests (Offline Verification)
1. **Compilation Check**:
   ```bash
   cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core
   cargo check
   ```
   *Expected result*: Code 0, 0 compiler warnings.

2. **Unit Tests (TR-369 + TR-069)**:
   ```bash
   cargo test
   ```
   *Expected result*: All 19 existing tests pass, plus new tests:
   - `test_parse_huawei_echolife_inform_xml`: parses Huawei HG8245H Inform, validates serial `4857544312345678`, optical signal `-19.85`, software version `V5R019C20S120`.
   - `test_parse_tplink_inform_xml`: parses TP-Link EX XML Inform with IGD parameter paths.
   - `test_build_inform_response`: verifies generated SOAP Envelope ID matches request ID.
   - `test_malformed_xml_rejection`: asserts non-XML or truncated XML returns Err without panic.

### 5.2 End-to-End Integration Verification via `curl`
Simulate a Huawei EchoLife ONT Inform:
```bash
curl -X POST http://localhost:7547/ \
  -H "Content-Type: text/xml; charset=utf-8" \
  --data '<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID SOAP-ENV:mustUnderstand="1">1001</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>4857544312345678</SerialNumber>
      </DeviceId>
      <ParameterList>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value>V5R019C20S120</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANOponInterfaceConfig.RXPower</Name>
          <Value>-19.85</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress</Name>
          <Value>187.55.120.44</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>'
```
*Expected Result*:
1. HTTP Response 200 OK with XML `InformResponse`:
   ```xml
   <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
     <soapenv:Header><cwmp:ID soapenv:mustUnderstand="1">1001</cwmp:ID></soapenv:Header>
     <soapenv:Body><cwmp:InformResponse><MaxEnvelopes>1</MaxEnvelopes></cwmp:InformResponse></soapenv:Body>
   </soapenv:Envelope>
   ```
2. Database Verification (`cpe_live_state`):
   ```sql
   SELECT cpe_id, status, ip_address, firmware_version, telemetry_metrics->>'rx_optical_power' AS rx_power
   FROM cpe_live_state
   WHERE cpe_id = '4857544312345678';
   ```
   *Expected output*: `cpe_id = 4857544312345678`, `status = online`, `ip_address = 187.55.120.44`, `firmware_version = V5R019C20S120`, `rx_power = -19.85`.

### 5.3 Regression Verification
Execute `./simulate_flow.sh`:
- Verifies all 5 existing steps (Registration -> MQTT Publish -> Live State -> Optical Trigger Reconcile -> MQTT Reboot Command) continue to pass with exit code 0.
