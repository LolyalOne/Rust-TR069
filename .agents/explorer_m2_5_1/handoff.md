# Architectural Investigation & Design Report: Embedded Axum CWMP Server (Milestone 2)

**Agent**: Explorer 1 (`explorer_m2_5_1`)  
**Milestone**: Milestone 2 — Embedded HTTP (CWMP) Server in Rust Core  
**Date**: 2026-09-07T19:19:00Z  
**Target Repository**: `Rust-TR069` (`rust-core/`)  

---

## 1. Observation

### 1.1 Existing Dependencies & Toolchain
From `rust-core/Cargo.toml` (lines 8–25):
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
```

- **Installed Toolchain**:
  - `rustc 1.98.1 (48a229cea 2026-09-01)`
  - `cargo 1.98.1 (797e8a9bc 2026-08-05)`
- **Locked Runtime Versions** (`rust-core/Cargo.lock` lines 1870–1884):
  - `tokio = "1.53.1"` (features: `bytes`, `libc`, `mio`, `parking_lot`, `pin-project-lite`, `signal-hook-registry`, `socket2`, `tokio-macros`)
  - `sqlx = "0.7.4"`
  - `memchr = "2.7.4"` (already present in tree via `aho-corasick`)

### 1.2 Dependency Dry-Run Resolution Results
Executed via `cargo add --dry-run` in `rust-core/`:
- **Axum 0.7**:
  ```
  Adding axum v0.7.7 to dependencies
  Features: form, http1, json, matched-path, original-uri, query, tokio, tower-log, tracing
  ```
- **Axum 0.8**:
  ```
  Adding axum v0.8.9 to dependencies
  Features: form, http1, json, matched-path, original-uri, query, tokio, tower-log, tracing
  ```
- **XML Parsers**:
  ```
  Adding roxmltree v0.21.1 to dependencies (Features: std, positions; depends only on existing memchr)
  Adding quick-xml v0.42.0 to dependencies
  ```
- **Tower / Hyper**:
  - Neither `hyper` nor `tower` need to be direct dependencies because `axum` re-exports all required HTTP types (`axum::http::{StatusCode, HeaderMap, header}`, `axum::body::Bytes`, `axum::serve`, `axum::extract::State`, `axum::response::IntoResponse`).

### 1.3 Current Tokio Runtime Lifecycle in `rust-core/src/main.rs`
Observed in `rust-core/src/main.rs` (lines 678–765):
1. **Entry Point & Channels**:
   - `let (tx, rx) = channel::<TelemetryUpdate>(channel_capacity);` (line 729)
   - `let (shutdown_tx, shutdown_rx) = watch::channel(false);` (line 730)
2. **Current Task Spawning**:
   - `run_healthcheck_monitor(health.clone(), db_pool.clone(), shutdown_rx.clone())` (lines 733–737)
   - `run_db_sink(db_pool.clone(), rx, health.clone())` (lines 740–744)
   - `run_mqtt_ingest(..., tx, health.clone(), shutdown_rx.clone())` (lines 747–754) — **Crucial observation**: `tx` is currently moved directly into `run_mqtt_ingest`!
3. **Shutdown Mechanism**:
   - `tokio::signal::ctrl_c().await` (line 757) triggers `shutdown_tx.send(true)` (line 760).
   - `run_mqtt_ingest` exits its loop upon `shutdown_rx.changed()`, disconnects MQTT client, and terminates (lines 592–597).
   - `run_db_sink` drains until `rx.recv().await` yields `None` (line 513).
   - `tokio::join!(mqtt_handle, db_handle, health_handle)` (line 761) awaits completion.

### 1.4 Milestone 1 Database & Environment Contracts
- `docker-compose.yml` (lines 54–58):
  - `CWMP_PORT=7547`
  - `CWMP_HOST=0.0.0.0`
  - Port mapping: `"7547:7547"`
- `postgres/init.sql` (lines 239–257):
  - Table `cpe_pending_commands`:
    ```sql
    CREATE TABLE IF NOT EXISTS cpe_pending_commands (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        cpe_id VARCHAR(128) NOT NULL REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
        command_type VARCHAR(64) NOT NULL,
        command_payload JSONB DEFAULT '{}'::jsonb,
        status VARCHAR(32) DEFAULT 'pending',
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        dispatched_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        result_payload JSONB
    );
    CREATE INDEX IF NOT EXISTS idx_cpe_pending_commands_lookup
        ON cpe_pending_commands (cpe_id, status, created_at);
    ```

---

## 2. Logic Chain

### 2.1 Dependency Selection Rationale
1. **Axum Version (`axum = "0.7"` vs `"0.8"`)**:
   - `axum 0.7` (resolved `0.7.7`+) is built on Hyper 1.0 and Tokio 1.x, fully matching `tokio = 1.38+`.
   - Its default features (`tokio`, `http1`, `json`, `tracing`) provide the exact toolset required for TR-069:
     - `tokio`: includes `tokio/net` and `axum::serve(listener, app)`
     - `http1`: standard HTTP/1.1 transport mandated by TR-069 Section 3.1
     - `axum::body::Bytes`: handles raw byte payloads without UTF-8 pre-validation errors
     - `axum::extract::State`: type-safe dependency injection for shared application state
   - Recommendation: `axum = "0.7"` (or `"0.8"` — both compile cleanly on the installed toolchain).
2. **XML Parsing Library (`roxmltree = "0.21"`)**:
   - TR-069 payloads contain vendor-specific XML namespace prefixes (`xmlns:cwmp="urn:dslforum-org:cwmp-1-0"`, `cwmp-1-2`, `cwmp-1-4`).
   - Serde-based XML deserializers (`quick-xml::de` or `serde-xml-rs`) frequently fail when vendor devices omit or vary namespace prefixes.
   - `roxmltree` parses an XML document into a read-only node tree. For every element, `node.tag_name().name()` returns the **local name** (e.g. `Inform`, `DeviceId`, `ParameterValueStruct`, `Name`, `Value`) irrespective of namespace prefix.
   - `roxmltree` has zero allocations beyond the node tree and relies only on `memchr`, which is already compiled in `rust-core`.
   - Handles self-closing `<Value xsi:type="xsd:string"/>` gracefully: `node.text().unwrap_or("")` produces an empty string without panicking.

### 2.2 Concurrency & Channel Ingress Convergence
1. Currently, `tx` (`Sender<TelemetryUpdate>`) is moved into `run_mqtt_ingest`.
2. To achieve dual-stack convergence without altering the PostgreSQL database sink:
   - In `main.rs`:
     ```rust
     let axum_tx = tx.clone();
     ```
   - `axum_tx` is passed to the Axum server's `AppState`.
   - `tx` is passed to `run_mqtt_ingest`.
   - When an ONT issues `cwmp:Inform`, the Axum handler parses the XML, generates a `TelemetryUpdate`, and submits it to `state.tx.send(update).await`.
   - Because `run_db_sink` already consumes from `rx`, both TR-369 USP messages (from MQTT) and TR-069 CWMP messages (from Axum) converge into the identical database pipeline.
   - The PostgreSQL `reconcile_live_to_history()` trigger operates without modification because `TelemetryUpdate.telemetry_metrics` carries `"rx_optical_power"`.

### 2.3 Graceful Shutdown Coordination
1. In Tokio, an MPSC channel closes when **all** `Sender` instances are dropped.
2. If `main` were to hold a reference to `tx`, `run_db_sink` would never complete during shutdown.
3. Therefore:
   - `main` creates `(tx, rx)`, clones `axum_tx = tx.clone()`, moves `tx` into `run_mqtt_ingest`, and moves `axum_tx` into `AppState`. `main` retains zero `Sender` handles.
   - Axum server is spawned using `axum::serve(listener, app).with_graceful_shutdown(signal)`.
   - The shutdown signal future resolves when `shutdown_rx.changed().await` sees `*shutdown_rx.borrow() == true`.
   - Upon `ctrl_c`:
     1. `shutdown_tx.send(true)` notifies both MQTT and Axum.
     2. Axum finishes active HTTP requests, stops accepting new connections, and exits its task, dropping `AppState` (and its `Sender`).
     3. MQTT disconnects and drops its `Sender`.
     4. Once both senders are dropped, `rx.recv().await` in `run_db_sink` returns `None`.
     5. `run_db_sink` drains all remaining queued updates to Postgres and terminates cleanly.
     6. `tokio::join!(mqtt_handle, axum_handle, db_handle, health_handle)` returns.

### 2.4 TR-069 Session & Pending Command Dispatch Architecture
In TR-069 (Broadband Forum TR-069 Section 3.7.1), communication is half-duplex where the CPE initiates the session:
1. **Phase 1 (CPE Request Phase — `cwmp:Inform`)**:
   - CPE sends HTTP POST `/` with `<cwmp:Inform>` containing `DeviceId` and `ParameterList`.
   - Axum handler extracts `cpe_id = SerialNumber`, optical metrics, parameters, and sends `TelemetryUpdate` to MPSC.
   - ACS returns HTTP 200 OK with `<cwmp:InformResponse>` and a session cookie (`Set-Cookie: session=<cpe_id>; Path=/`).
2. **Phase 2 (ACS Request Phase — Empty HTTP POST)**:
   - CPE sends an empty HTTP POST (`Content-Length: 0` or empty body) with the session cookie.
   - Axum handler extracts `cpe_id` from the cookie (or fallback IP lookup in `cpe_live_state`).
   - Handler queries `cpe_pending_commands`:
     ```sql
     SELECT id::text AS id, command_type, command_payload
     FROM cpe_pending_commands
     WHERE cpe_id = $1 AND status = 'pending'
     ORDER BY created_at ASC
     LIMIT 1
     FOR UPDATE SKIP LOCKED;
     ```
   - **If command exists**:
     - Marks command as `dispatched`:
       ```sql
       UPDATE cpe_pending_commands
       SET status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP
       WHERE id = $1::uuid;
       ```
     - Returns HTTP 200 OK with SOAP RPC payload (e.g. `<cwmp:Reboot>` or `<cwmp:GetParameterValues>`).
   - **If no command exists**:
     - Returns Empty HTTP 200 OK (`Content-Length: 0`).
     - This signals session termination to the CPE.
3. **Phase 3 (CPE RPC Response)**:
   - CPE sends HTTP POST with `<cwmp:RebootResponse>`, `<cwmp:GetParameterValuesResponse>`, or `<SOAP-ENV:Fault>`.
   - Handler matches command ID, updates `cpe_pending_commands` to `completed` (or `failed`), and checks for subsequent commands.

---

## 3. Caveats

1. **Session Identification on Empty POST**:
   - TR-069 specification stipulates that CPEs preserve cookies across HTTP requests within the same TCP session. However, low-end consumer ONTs with buggy HTTP clients occasionally drop cookies on the empty POST.
   - *Mitigation*: The Axum server must support dual resolution:
     1. Primary: Extract `cpe_id` from `Cookie: session=<cpe_id>`.
     2. Fallback: Lookup client IP in `cpe_live_state` (`WHERE ip_address = $1 ORDER BY last_seen DESC LIMIT 1`) or an in-memory `DashMap<IpAddr, (String, Instant)>`.
2. **Body Limit in Axum**:
   - Default Axum body limit is 2 MB (`DefaultBodyLimit`). A typical TR-069 Inform with ~100 parameters is between 15 KB and 45 KB. 2 MB is more than sufficient, but if extreme vendor dumps occur, an explicit `DefaultBodyLimit::max(10 * 1024 * 1024)` can be configured to prevent 413 Payload Too Large.
3. **Non-UTF-8 Encoding**:
   - Broadband Forum specifies UTF-8, but some older firmware may emit Latin-1. The parser should convert using `String::from_utf8_lossy(&body)` rather than `std::str::from_utf8(&body).unwrap()`, guaranteeing that malformed character bytes never trigger an internal server error.
4. **PostgreSQL UUID Dialect in Rust**:
   - In `cpe_pending_commands`, the `id` column is a native PostgreSQL `UUID`.
   - In `sqlx`, querying `SELECT id::text AS id, ...` allows reading `id` directly into a Rust `String` without requiring `uuid = { version = "1.0" }` or enabling the `"uuid"` feature flag on `sqlx`. Alternatively, adding `"uuid"` to `sqlx` is also clean. Using `id::text` is recommended for zero dependency friction.

---

## 4. Conclusion & Architectural Recommendations for the Worker

### 4.1 Recommended `rust-core/Cargo.toml` Edits
Add the following dependencies under `[dependencies]`:
```toml
# Embedded CWMP HTTP Server & XML Parsing (Milestone 2)
axum = "0.7"
roxmltree = "0.21"
```

### 4.2 Modular Code Architecture
Rather than placing all CWMP logic into an already large `main.rs` (1247 lines), create a dedicated submodule `rust-core/src/cwmp/`:
```
rust-core/src/
├── main.rs
├── cwmp/
│   ├── mod.rs        # Public interface: CwmpServer, AppState, run_cwmp_server
│   ├── parser.rs     # roxmltree-based TR-069 Inform & Response parser
│   ├── handlers.rs   # Axum route handlers (POST /, /cwmp, /tr069)
│   └── commands.rs   # Pending command queries and SOAP RPC builders
```
*(Alternatively, a single file `rust-core/src/cwmp.rs` can be declared with `mod cwmp;` in `main.rs`).*

### 4.3 AppState & Server Signature
```rust
#[derive(Clone)]
pub struct AppState {
    pub tx: Sender<TelemetryUpdate>,
    pub db_pool: PgPool,
}

pub async fn run_cwmp_server(
    listener: tokio::net::TcpListener,
    state: AppState,
    mut shutdown_rx: watch::Receiver<bool>,
) -> Result<()> {
    let app = axum::Router::new()
        .route("/", axum::routing::post(handlers::handle_cwmp_post))
        .route("/cwmp", axum::routing::post(handlers::handle_cwmp_post))
        .route("/tr069", axum::routing::post(handlers::handle_cwmp_post))
        .with_state(state);

    let shutdown_signal = async move {
        while shutdown_rx.changed().await.is_ok() {
            if *shutdown_rx.borrow() {
                break;
            }
        }
    };

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal)
        .await
        .context("CWMP Axum server encountered fatal error")?;

    tracing::info!("CWMP HTTP Server shutdown cleanly");
    Ok(())
}
```

### 4.4 Bootstrap in `main.rs`
```rust
// 1. Read CWMP configuration
let cwmp_host = std::env::var("CWMP_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
let cwmp_port: u16 = std::env::var("CWMP_PORT")
    .unwrap_or_else(|_| "7547".to_string())
    .parse()
    .unwrap_or(7547);
let cwmp_addr = format!("{}:{}", cwmp_host, cwmp_port);

// 2. Bind listener early to detect port conflicts on startup
let cwmp_listener = tokio::net::TcpListener::bind(&cwmp_addr)
    .await
    .with_context(|| format!("Failed to bind CWMP HTTP server to {}", cwmp_addr))?;
tracing::info!(addr = %cwmp_addr, "Bound CWMP HTTP Server listener");

// 3. Channels and state
let (tx, rx) = channel::<TelemetryUpdate>(channel_capacity);
let (shutdown_tx, shutdown_rx) = watch::channel(false);

let app_state = AppState {
    tx: tx.clone(),
    db_pool: db_pool.clone(),
};

// 4. Spawn tasks
let health_handle = tokio::spawn(run_healthcheck_monitor(health.clone(), db_pool.clone(), shutdown_rx.clone()));
let db_handle = tokio::spawn(run_db_sink(db_pool.clone(), rx, health.clone()));
let mqtt_handle = tokio::spawn(run_mqtt_ingest(mqtt_host, mqtt_port, "rust-usp-core".to_string(), tx, health.clone(), shutdown_rx.clone()));
let cwmp_handle = tokio::spawn(run_cwmp_server(cwmp_listener, app_state, shutdown_rx.clone()));

// 5. Shutdown await
tokio::signal::ctrl_c().await.context("Failed to listen for ctrl_c signal")?;
tracing::info!("Received shutdown signal. Initiating graceful shutdown...");
let _ = shutdown_tx.send(true);

let _ = tokio::join!(mqtt_handle, cwmp_handle, db_handle, health_handle);
tracing::info!("Dual-Stack Rust Core Worker shutdown complete.");
```

### 4.5 Inform Parsing & Normalization Specification
The XML parser must extract:
- `cpe_id`: `<DeviceId><SerialNumber>` (e.g. `485754431234ABCD` for Huawei, `22081B12345678` for TP-Link)
- `current_parameters`: Key-value JSON map of all `<ParameterValueStruct>` elements
- `rx_optical_power`:
  - Check for keys containing `Optical` and (`X_HW_OpticalRxPower`, `OpticalSignalLevel`, `RxPower`).
  - Strip `" dBm"` or whitespace.
  - If string contains `.`, parse directly as `f64`.
  - If integer `< -10000`, divide by 1000.0 (`-19500 -> -19.50`).
  - If integer `< -100`, divide by 100.0 (`-1950 -> -19.50`).
  - Insert into `telemetry_metrics["rx_optical_power"]`.
- `firmware_version`: `<SoftwareVersion>` or `*SoftwareVersion` parameter.
- `ip_address`: `*ConnectionRequestURL` host or client socket IP.

---

## 5. Verification Method

To independently verify the implementation once the Worker writes the code:

### 5.1 Cargo Build & Unit Tests
```bash
cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core
cargo test -- --nocapture
```
*Expected Result*:
- Zero compile errors with `axum` and `roxmltree`.
- All 19 pre-existing tests pass.
- All newly added CWMP XML parsing unit tests (Huawei EchoLife, TP-Link, namespace variance, self-closing tags) pass.

### 5.2 Server Binding & Port Exposure Verification
```bash
# Verify process listens on 7547
ss -tulpn | grep 7547
```

### 5.3 Full CWMP Handshake Simulation via `curl`
```bash
# 1. Inform Request (Huawei EchoLife)
curl -i -X POST http://127.0.0.1:7547/ \
  -H "Content-Type: text/xml; charset=utf-8" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID mustUnderstand="1">10001</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd.</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>485754431234ABCD</SerialNumber>
      </DeviceId>
      <ParameterList>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value>-19.50</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>'
```
*Expected Result*:
- HTTP 200 OK.
- Response body contains `<cwmp:InformResponse>`.
- Header contains `Set-Cookie: session=485754431234ABCD...`.

```bash
# 2. Empty POST (Shift to ACS Request Phase)
curl -i -X POST http://127.0.0.1:7547/ \
  -H "Cookie: session=485754431234ABCD" \
  -H "Content-Length: 0"
```
*Expected Result*:
- If pending command queued: HTTP 200 OK with `<cwmp:Reboot>` or `<cwmp:GetParameterValues>`.
- If no command queued: HTTP 200 OK with `Content-Length: 0`.

### 5.4 Invalidation Conditions
- If `axum::serve` fails to exit during graceful shutdown, `run_db_sink` will hang in `tokio::join!`.
- If `cpe_id` extraction from empty POST fails, pending commands will not be dispatched.
- If optical power string parsing ignores the `" dBm"` suffix or scaling factors, `cpe_historical_metrics` trigger will not fire.
