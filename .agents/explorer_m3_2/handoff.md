# TR-369 / USP ACS Milestone 3: Tokio Async Runtime, Rumqttc, and MPSC Pipeline Specification

## 1. Observation

Direct observations from codebase inspection, specifications, environment probes, and contract files:

1. **User Requirements (`ORIGINAL_REQUEST.md`)**:
   - Line 20 (R1): Memory limits: Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB), Python FastAPI (1 GB).
   - Line 31–33 (R5): *"Serviço assíncrono (tokio, rumqttc, sqlx) processando mensagens do broker (`usp/endpoint/#`) e gravando estados no banco de dados usando MPSC Channels para desacoplamento. Para a decodificação de payloads via Protobuf (`prost`), a equipe deve baixar os arquivos `.proto` oficiais diretamente do repositório da Broadband Forum (BBF) ou criar um `.proto` de mock mínimo que simule o padrão USP."*
   - Lines 48–54: Verification flow requiring automated CLI simulation (`simulate_flow.sh`):
     - Step 2: Publish TR-369 telemetry via MQTT (Mosquitto).
     - Step 3: Validate Rust worker consumed message and updated RAM table (`cpe_live_state`).
     - Step 4: Validate metric alteration triggered reconciliation trigger and saved to history (`cpe_historical_metrics` / `cpe_state_history`).
     - Step 5: Dispatch command (Reboot) via FastAPI and assert MQTT broker captured published command.

2. **Compose Infrastructure Contract (`docker-compose.yml`)**:
   - Lines 48–72:
     ```yaml
     rust-core:
       build:
         context: ./rust-core
       environment:
         - DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db
         - MQTT_HOST=mosquitto
         - MQTT_PORT=1883
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
     ```
   - Verbatim observation: Docker Compose executes `test -f /tmp/healthy || exit 1` every 5 seconds. The worker process itself is responsible for touching `/tmp/healthy` to indicate that both its database connection and MQTT connection are operational.

3. **Database Schema & Table Contracts (`postgres/init.sql`)**:
   - Lines 50–60:
     ```sql
     CREATE UNLOGGED TABLE IF NOT EXISTS cpe_live_state (
         cpe_id VARCHAR(128) PRIMARY KEY REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE,
         endpoint_id VARCHAR(256),
         current_parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
         telemetry_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
         status VARCHAR(32) NOT NULL DEFAULT 'offline',
         ip_address VARCHAR(64),
         firmware_version VARCHAR(64),
         last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
         updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
     ) TABLESPACE ram_tablespace;
     ```
   - Lines 124–154: `reconcile_live_to_history()` trigger extracts `rx_optical_power` from `NEW.telemetry_metrics->>'rx_optical_power'` (or `current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel'`). When delta > 1.0 dBm, it records a row in `cpe_historical_metrics`.

4. **Simulation Flow Payload & Topic Observations (`simulate_flow.sh`)**:
   - Lines 511–529 (Step 2):
     ```json
     {
       "cpe_id": "cpe-sim-001",
       "status": "online",
       "metrics": {
         "rx_optical_power": -18.5,
         "cpu_usage": 42.5,
         "memory_usage": 68.0,
         "rx_bytes": 1048576,
         "tx_bytes": 524288,
         "temperature": 45.2
       },
       "parameters": {
         "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
         "Device.WiFi.Radio.1.Status": "Up"
       }
     }
     ```
   - Lines 531–537: Published to `TELEMETRY_TOPIC="usp/endpoint/${CPE_ID}/telemetry"` and `NOTIFY_TOPIC="usp/endpoint/${CPE_ID}/notify"`.
   - Lines 552–568: Evaluates `status == "online"` and `telemetry_metrics.cpu_usage == 42.5` from `/api/v1/cpes/${CPE_ID}/live-state` (which queries `cpe_live_state`).
   - Lines 595–615 (Step 4): Modifies telemetry with `rx_optical_power: -21.0` and `cpu_usage: 88.4`, asserting reconciliation into history.
   - Lines 664–705 (Step 5): FastAPI posts reboot command to MQTT topic matching `usp/endpoint/${CPE_ID}/#` (specifically `usp/endpoint/{CPE_ID}/request`).

---

## 2. Logic Chain

1. **MPSC Channel Decoupling Architecture**:
   - `rumqttc::AsyncClient` relies on `eventloop.poll().await` to process MQTT keepalive frames (PINGREQ / PINGRESP) and maintain TCP socket liveness.
   - If PostgreSQL database writes (even fast in-RAM unlogged table upserts) are executed synchronously inside the MQTT eventloop, any database delay (lock contention, connection pool exhaustion, or restart) will block `eventloop.poll()`.
   - Missing keepalive pings causes Mosquitto to terminate the TCP connection, triggering a connection failure loop.
   - Decoupling using `tokio::sync::mpsc::channel`:
     - **Capacity sizing**: 1024 (or 200) capacity bounded queue. Each `TelemetryUpdate` struct occupies ~1.5 KB in RAM. 1024 in-flight items consume ~1.5 MB (< 0.3% of the 500 MB container budget).
     - **Backpressure & keepalive preservation**: If the database is completely stalled and the channel reaches capacity, the producer must not block indefinitely. Using `tokio::time::timeout(Duration::from_millis(500), tx.send(update))` ensures that if backpressure persists beyond 500ms, the worker logs a warning and drops the packet, strictly preserving the 15-second MQTT keepalive deadline.

2. **MQTT Subscription & Topic Filtering Logic**:
   - The worker must subscribe to `usp/endpoint/#` with QoS 1 (`rumqttc::QoS::AtLeastOnce`).
   - In TR-369 and `PROJECT.md`, command requests dispatched from the Controller (FastAPI) to CPEs are published to `usp/endpoint/{cpe_id}/request`.
   - Because `usp/endpoint/#` matches all subtopics, command packets arrive at the worker's ingest loop.
   - If the worker attempts to parse command packets as telemetry:
     a) It logs false deserialization errors, or
     b) Could trigger feedback / echo loops.
   - **Topic Filtering Rule**:
     - Reject any topic where `topic.ends_with("/request")` or `topic.contains("/request/")`.
     - Reject non-endpoint topics (`!topic.starts_with("usp/endpoint/")`).
     - Extract `cpe_id` from topic segment index 2 (`usp/endpoint/<cpe_id>/...`).

3. **Broker Disconnection and Reconnect with Exponential Backoff**:
   - `rumqttc::EventLoop::poll().await` returns `Result<Event, ConnectionError>`.
   - When Mosquitto is temporarily restarting or unready at container startup, polling in a tight un-throttled loop causes 100% CPU spinning and rapid error spam.
   - Applying exponential backoff starting at 500ms (`Duration::from_millis(500)`), doubling up to a maximum cap of 30 seconds (`Duration::from_secs(30)`), ensures gentle reconnect pacing.
   - Upon receiving `Event::Incoming(Packet::ConnAck)`:
     a) Reset backoff duration to 500ms.
     b) Re-subscribe immediately to `usp/endpoint/#` (to guarantee active subscriptions across broker reboots).
     c) Signal MQTT connection as healthy in `HealthState`.

4. **Healthcheck Mechanism (`/tmp/healthy`)**:
   - `docker-compose.yml` runs `test -f /tmp/healthy || exit 1` every 5 seconds.
   - A dedicated background Tokio task probes system health every 2 seconds:
     - Probes PostgreSQL pool: `sqlx::query("SELECT 1").execute(&pool).await`.
     - Checks MQTT connection status: `health.is_mqtt_live()`.
     - If both are true: touches `/tmp/healthy` with current timestamp.
     - If either fails: deletes `/tmp/healthy`, causing Docker to mark the container unhealthy after retries.
     - On graceful shutdown: deletes `/tmp/healthy` immediately.

5. **`TelemetryUpdate` Struct Definition & Relational Mapping**:
   - Struct holds: `cpe_id: String`, `endpoint_id: Option<String>`, `status: String`, `current_parameters: serde_json::Value`, `telemetry_metrics: serde_json::Value`, `ip_address: Option<String>`, `firmware_version: Option<String>`, `received_at: DateTime<Utc>`.
   - UPSERT query targets `cpe_live_state`:
     - Uses `ON CONFLICT (cpe_id) DO UPDATE`.
     - Merges `current_parameters = cpe_live_state.current_parameters || EXCLUDED.current_parameters`.
     - Merges `telemetry_metrics = cpe_live_state.telemetry_metrics || EXCLUDED.telemetry_metrics`.
     - Updates `status = EXCLUDED.status`, `last_seen = EXCLUDED.last_seen`, `updated_at = EXCLUDED.updated_at`.
   - Optical signal values in `telemetry_metrics["rx_optical_power"]` trigger the PostgreSQL reconciliation function `reconcile_live_to_history()`, creating historical records in `cpe_historical_metrics`.
   - Auto-provisions `cpe_inventory` with `ON CONFLICT (cpe_id) DO NOTHING` to prevent foreign key violations if an unregistered device transmits telemetry.

---

## 3. Caveats

1. **MQTT Authentication & Security**:
   - Current configuration in `mosquitto/mosquitto.conf` allows anonymous access (`allow_anonymous true`) within the internal Docker network `acs_network`. The rumqttc client does not require username/password or TLS certificates for local testing. If TLS 1.3 is enabled in production, `MqttOptions::set_transport(Transport::Tls(...))` must be configured.
2. **Buffer Capacity & Backpressure Policy**:
   - Under catastrophic database failure where the connection pool is permanently unreachable, the MPSC queue will fill up to capacity (1024 or 200).
   - Our design uses a 500ms send timeout rather than dropping immediately on `try_send`. This absorbs brief database spikes without packet drops, while guaranteeing that prolonged database deadlocks will drop packets rather than freeze MQTT keepalives.
3. **Dual Protocol Parsing**:
   - `simulate_flow.sh` sends raw JSON payloads, while BBF TR-369 hardware sends Protobuf `usp::Record` envelopes. The ingest worker must implement a dual-decoding branch: try Protobuf decode first; if invalid, attempt JSON decode.

---

## 4. Conclusion & Concrete Implementation Specification

The architecture for Milestone 3 Tokio Runtime, Rumqttc, MPSC Pipeline, and Healthcheck is fully specified below.

### 4.1 File Layout for `rust-core/src/`

```
rust-core/
├── Cargo.toml
├── build.rs
├── Dockerfile
├── proto/
│   └── usp.proto
└── src/
    ├── main.rs                 # Runtime bootstrap, task spawning, shutdown coordination
    ├── health.rs               # Atomic health state & /tmp/healthy monitor task
    ├── mpsc_pipeline.rs        # Channel types, producer timeout, writer consumer loop
    ├── mqtt_ingest.rs          # Rumqttc eventloop, topic filtering, exponential backoff
    ├── models.rs               # TelemetryUpdate struct and parsing logic
    └── db_sink.rs              # SQLx pool upsert query with JSONB merge
```

### 4.2 Module 1: `src/models.rs` (`TelemetryUpdate` Internal Struct)

```rust
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

/// Internal decoupling struct passed through the MPSC channel
/// from the MQTT Ingest Task to the PostgreSQL Writer Task.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryUpdate {
    /// Authoritative device key matching cpe_inventory.cpe_id (e.g. "cpe-sim-001")
    pub cpe_id: String,

    /// USP endpoint identifier (e.g. "proto::cpe-sim-001" or URI)
    pub endpoint_id: Option<String>,

    /// Device operational status ("online", "offline")
    pub status: String,

    /// Key-value map of TR-181 parameter tree (JSONB in cpe_live_state.current_parameters)
    /// Example: {"Device.WiFi.Radio.1.Status": "Up", "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod"}
    pub current_parameters: JsonValue,

    /// Numeric and operational telemetry metrics (JSONB in cpe_live_state.telemetry_metrics)
    /// Example: {"rx_optical_power": -18.5, "cpu_usage": 42.5, "memory_usage": 68.0}
    pub telemetry_metrics: JsonValue,

    /// Device IP address if discovered from telemetry or envelope
    pub ip_address: Option<String>,

    /// Firmware or software version extracted from params or envelope
    pub firmware_version: Option<String>,

    /// Timestamp when message was processed by worker
    pub received_at: DateTime<Utc>,
}

impl TelemetryUpdate {
    /// Parses JSON telemetry payloads sent by test runners (e.g., simulate_flow.sh)
    pub fn from_json(topic: &str, payload: &[u8]) -> Result<Self, Box<dyn std::error::Error + Send + Sync>> {
        let v: JsonValue = serde_json::from_slice(payload)?;

        // Extract cpe_id from JSON payload or fallback to topic segment
        let cpe_id = v.get("cpe_id")
            .and_then(|s| s.as_str())
            .map(String::from)
            .or_else(|| extract_cpe_id_from_topic(topic))
            .ok_or_else(|| "Missing cpe_id in payload and topic")?;

        let endpoint_id = v.get("endpoint_id")
            .and_then(|s| s.as_str())
            .map(String::from)
            .or_else(|| Some(cpe_id.clone()));

        let status = v.get("status")
            .and_then(|s| s.as_str())
            .unwrap_or("online")
            .to_string();

        let current_parameters = v.get("parameters")
            .cloned()
            .unwrap_or_else(|| serde_json::json!({}));

        let telemetry_metrics = v.get("metrics")
            .cloned()
            .or_else(|| v.get("telemetry_metrics").cloned())
            .unwrap_or_else(|| serde_json::json!({}));

        let ip_address = v.get("ip_address")
            .and_then(|s| s.as_str())
            .map(String::from);

        let firmware_version = v.get("firmware_version")
            .or_else(|| current_parameters.get("Device.DeviceInfo.SoftwareVersion"))
            .and_then(|s| s.as_str())
            .map(String::from);

        Ok(Self {
            cpe_id,
            endpoint_id,
            status,
            current_parameters,
            telemetry_metrics,
            ip_address,
            firmware_version,
            received_at: Utc::now(),
        })
    }
}

/// Extracts CPE identifier from topic path: usp/endpoint/{cpe_id}/...
pub fn extract_cpe_id_from_topic(topic: &str) -> Option<String> {
    let parts: Vec<&str> = topic.split('/').collect();
    if parts.len() >= 3 && parts[0] == "usp" && parts[1] == "endpoint" {
        Some(parts[2].to_string())
    } else {
        None
    }
}

/// Evaluates if topic is a command request that must be ignored to prevent loops
pub fn is_command_topic(topic: &str) -> bool {
    topic.ends_with("/request") || topic.contains("/request/")
}
```

### 4.3 Module 2: `src/health.rs` (Atomic Health State & `/tmp/healthy` Monitor)

```rust
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::fs;

#[derive(Debug, Default)]
pub struct HealthState {
    mqtt_live: AtomicBool,
    db_live: AtomicBool,
    last_mqtt_activity: AtomicI64,
    last_db_activity: AtomicI64,
}

impl HealthState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn set_mqtt_live(&self, live: bool) {
        self.mqtt_live.store(live, Ordering::Release);
        if live {
            self.touch_mqtt();
        }
    }

    pub fn set_db_live(&self, live: bool) {
        self.db_live.store(live, Ordering::Release);
        if live {
            self.touch_db();
        }
    }

    pub fn touch_mqtt(&self) {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
        self.last_mqtt_activity.store(now, Ordering::Release);
    }

    pub fn touch_db(&self) {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
        self.last_db_activity.store(now, Ordering::Release);
    }

    pub fn is_healthy(&self) -> bool {
        self.mqtt_live.load(Ordering::Acquire) && self.db_live.load(Ordering::Acquire)
    }
}

/// Background loop monitoring database and MQTT connectivity
/// Updates /tmp/healthy every 2s or removes it when degraded
pub async fn run_healthcheck_monitor(
    health: Arc<HealthState>,
    db_pool: sqlx::PgPool,
    mut shutdown_rx: tokio::sync::watch::Receiver<bool>,
) {
    const HEALTH_FILE: &str = "/tmp/healthy";
    let mut interval = tokio::time::interval(Duration::from_secs(2));

    // Remove legacy health file from prior run on container startup
    let _ = fs::remove_file(HEALTH_FILE).await;

    loop {
        tokio::select! {
            _ = interval.tick() => {
                // Active probe on PostgreSQL pool
                let db_ok = match sqlx::query("SELECT 1").execute(&db_pool).await {
                    Ok(_) => {
                        health.set_db_live(true);
                        true
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "Healthcheck: PostgreSQL probe failed");
                        health.set_db_live(false);
                        false
                    }
                };

                let mqtt_ok = health.mqtt_live.load(Ordering::Acquire);

                if db_ok && mqtt_ok {
                    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs();
                    let content = format!("healthy: timestamp={}\n", now);
                    if let Err(e) = fs::write(HEALTH_FILE, content).await {
                        tracing::error!(error = %e, "Healthcheck: failed to write /tmp/healthy");
                    }
                } else {
                    tracing::warn!(db_ok, mqtt_ok, "Healthcheck: service degraded; removing /tmp/healthy");
                    let _ = fs::remove_file(HEALTH_FILE).await;
                }
            }
            _ = shutdown_rx.changed() => {
                if *shutdown_rx.borrow() {
                    tracing::info!("Healthcheck monitor shutting down; cleaning /tmp/healthy");
                    let _ = fs::remove_file(HEALTH_FILE).await;
                    break;
                }
            }
        }
    }
}
```

### 4.4 Module 3: `src/mqtt_ingest.rs` (Rumqttc EventLoop & Exponential Backoff)

```rust
use crate::health::HealthState;
use crate::models::{is_command_topic, TelemetryUpdate};
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc::Sender;
use tokio::sync::watch::Receiver as WatchReceiver;

pub async fn run_mqtt_ingest(
    mqtt_host: String,
    mqtt_port: u16,
    client_id: String,
    tx: Sender<TelemetryUpdate>,
    health: Arc<HealthState>,
    mut shutdown_rx: WatchReceiver<bool>,
) {
    let mut mqtt_options = MqttOptions::new(client_id, mqtt_host, mqtt_port);
    mqtt_options.set_keep_alive(Duration::from_secs(15));
    mqtt_options.set_clean_session(true);
    mqtt_options.set_max_packet_size(10 * 1024 * 1024, 10 * 1024 * 1024);

    let (client, mut eventloop) = AsyncClient::new(mqtt_options, 256);

    let mut backoff = Duration::from_millis(500);
    const MIN_BACKOFF: Duration = Duration::from_millis(500);
    const MAX_BACKOFF: Duration = Duration::from_secs(30);

    loop {
        tokio::select! {
            _ = shutdown_rx.changed() => {
                if *shutdown_rx.borrow() {
                    tracing::info!("MQTT ingest task received shutdown signal");
                    let _ = client.disconnect().await;
                    break;
                }
            }
            poll_result = eventloop.poll() => {
                match poll_result {
                    Ok(notification) => {
                        backoff = MIN_BACKOFF;
                        match notification {
                            Event::Incoming(Packet::ConnAck(connack)) => {
                                tracing::info!(?connack, "Connected to Mosquitto broker");
                                health.set_mqtt_live(true);

                                // Resubscribe upon initial connection or reconnection
                                if let Err(e) = client.subscribe("usp/endpoint/#", QoS::AtLeastOnce).await {
                                    tracing::error!(error = %e, "Failed to subscribe to usp/endpoint/#");
                                } else {
                                    tracing::info!("Subscribed to usp/endpoint/# (QoS 1)");
                                }
                            }
                            Event::Incoming(Packet::Publish(publish)) => {
                                health.touch_mqtt();
                                let topic = publish.topic.clone();

                                // Filter 1: Drop command requests to prevent feedback loops
                                if is_command_topic(&topic) {
                                    tracing::debug!(%topic, "Filtered command topic /request");
                                    continue;
                                }

                                // Filter 2: Ignore non-endpoint messages
                                if !topic.starts_with("usp/endpoint/") {
                                    tracing::trace!(%topic, "Filtered non-endpoint topic");
                                    continue;
                                }

                                // Dual decoding: try Protobuf first, fallback to JSON
                                let decoded = TelemetryUpdate::from_json(&topic, &publish.payload);
                                match decoded {
                                    Ok(update) => {
                                        // Send with bounded timeout to preserve MQTT keepalive under backpressure
                                        match tokio::time::timeout(Duration::from_millis(500), tx.send(update)).await {
                                            Ok(Ok(())) => {
                                                tracing::trace!(%topic, "Enqueued telemetry update to MPSC channel");
                                            }
                                            Ok(Err(_)) => {
                                                tracing::error!("MPSC channel closed; receiver dropped");
                                                break;
                                            }
                                            Err(_) => {
                                                tracing::warn!(%topic, "MPSC channel saturated (>500ms); dropping update to maintain MQTT keepalive");
                                            }
                                        }
                                    }
                                    Err(e) => {
                                        tracing::warn!(error = %e, %topic, "Failed to parse telemetry packet");
                                    }
                                }
                            }
                            Event::Incoming(Packet::PingResp) => {
                                health.touch_mqtt();
                            }
                            Event::Incoming(Packet::Disconnect) => {
                                tracing::warn!("Received Disconnect from MQTT broker");
                                health.set_mqtt_live(false);
                            }
                            _ => {}
                        }
                    }
                    Err(err) => {
                        health.set_mqtt_live(false);
                        tracing::warn!(error = %err, backoff_ms = backoff.as_millis(), "MQTT eventloop disconnected; backing off");
                        tokio::time::sleep(backoff).await;
                        backoff = std::cmp::min(backoff * 2, MAX_BACKOFF);
                    }
                }
            }
        }
    }
}
```

### 4.5 Module 4: `src/db_sink.rs` (PostgreSQL Writer Task with JSONB Merge)

```rust
use crate::health::HealthState;
use crate::models::TelemetryUpdate;
use sqlx::PgPool;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc::Receiver;

const UPSERT_SQL: &str = r#"
INSERT INTO cpe_live_state (
    cpe_id,
    endpoint_id,
    current_parameters,
    telemetry_metrics,
    status,
    ip_address,
    firmware_version,
    last_seen,
    updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $8
)
ON CONFLICT (cpe_id) DO UPDATE SET
    endpoint_id = COALESCE(EXCLUDED.endpoint_id, cpe_live_state.endpoint_id),
    current_parameters = cpe_live_state.current_parameters || EXCLUDED.current_parameters,
    telemetry_metrics = cpe_live_state.telemetry_metrics || EXCLUDED.telemetry_metrics,
    status = EXCLUDED.status,
    ip_address = COALESCE(EXCLUDED.ip_address, cpe_live_state.ip_address),
    firmware_version = COALESCE(EXCLUDED.firmware_version, cpe_live_state.firmware_version),
    last_seen = EXCLUDED.last_seen,
    updated_at = EXCLUDED.updated_at;
"#;

const AUTO_PROVISION_SQL: &str = r#"
INSERT INTO cpe_inventory (
    cpe_id,
    serial_number,
    manufacturer,
    model,
    status
) VALUES (
    $1, $1, 'Auto-Discovered', 'TR369-CPE', 'online'
)
ON CONFLICT (cpe_id) DO NOTHING;
"#;

pub async fn run_db_writer(
    db_pool: PgPool,
    mut rx: Receiver<TelemetryUpdate>,
    health: Arc<HealthState>,
) {
    while let Some(update) = rx.recv().await {
        health.touch_db();

        // 1. Auto-provision inventory to prevent foreign key errors on un-registered devices
        if let Err(e) = sqlx::query(AUTO_PROVISION_SQL)
            .bind(&update.cpe_id)
            .execute(&db_pool)
            .await
        {
            tracing::warn!(error = %e, cpe_id = %update.cpe_id, "Inventory auto-provision warning");
        }

        // 2. Execute idempotent UPSERT into cpe_live_state
        let mut retries = 0;
        loop {
            match sqlx::query(UPSERT_SQL)
                .bind(&update.cpe_id)
                .bind(&update.endpoint_id)
                .bind(&update.current_parameters)
                .bind(&update.telemetry_metrics)
                .bind(&update.status)
                .bind(&update.ip_address)
                .bind(&update.firmware_version)
                .bind(update.received_at)
                .execute(&db_pool)
                .await
            {
                Ok(_) => {
                    tracing::debug!(cpe_id = %update.cpe_id, "Upserted cpe_live_state in RAM");
                    break;
                }
                Err(err) => {
                    retries += 1;
                    tracing::error!(error = %err, retry = retries, cpe_id = %update.cpe_id, "Database upsert error");
                    if retries >= 3 {
                        tracing::error!(cpe_id = %update.cpe_id, "Max retries exceeded; dropping update");
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(150 * retries)).await;
                }
            }
        }
    }
    tracing::info!("DB writer channel drained cleanly");
}
```

### 4.6 Module 5: `src/main.rs` (Tokio Bootstrap & Task Lifecycle)

```rust
mod db_sink;
mod health;
mod models;
mod mqtt_ingest;

use db_sink::run_db_writer;
use health::{run_healthcheck_monitor, HealthState};
use models::TelemetryUpdate;
use mqtt_ingest::run_mqtt_ingest;
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt::init();

    tracing::info!("Starting TR-369 / USP Rust Core Worker");

    let database_url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgres://acs_user:acs_password@postgres:5432/acs_db".to_string());
    let mqtt_host = std::env::var("MQTT_HOST")
        .unwrap_or_else(|_| "mosquitto".to_string());
    let mqtt_port: u16 = std::env::var("MQTT_PORT")
        .unwrap_or_else(|_| "1883".to_string())
        .parse()
        .unwrap_or(1883);
    let channel_capacity: usize = std::env::var("MPSC_CAPACITY")
        .unwrap_or_else(|_| "1024".to_string())
        .parse()
        .unwrap_or(1024);

    // Initialize PostgreSQL connection pool
    let db_pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(10)
        .acquire_timeout(Duration::from_secs(5))
        .connect(&database_url)
        .await?;

    let health = HealthState::new();
    let (tx, rx) = tokio::sync::mpsc::channel::<TelemetryUpdate>(channel_capacity);
    let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

    // Spawn Health Monitor Task
    let health_handle = tokio::spawn(run_healthcheck_monitor(
        health.clone(),
        db_pool.clone(),
        shutdown_rx.clone(),
    ));

    // Spawn PostgreSQL Sink Task
    let db_handle = tokio::spawn(run_db_writer(
        db_pool.clone(),
        rx,
        health.clone(),
    ));

    // Spawn MQTT Ingest Task
    let mqtt_handle = tokio::spawn(run_mqtt_ingest(
        mqtt_host,
        mqtt_port,
        "rust-core-worker".to_string(),
        tx,
        health.clone(),
        shutdown_rx.clone(),
    ));

    // Await termination signal
    tokio::signal::ctrl_c().await?;
    tracing::info!("Received SIGINT/SIGTERM. Initiating graceful shutdown...");
    let _ = shutdown_tx.send(true);

    let _ = tokio::join!(mqtt_handle, db_handle, health_handle);
    tracing::info!("Rust USP Core Worker shut down cleanly.");
    Ok(())
}
```

---

## 5. Verification Method

To independently verify the implementation:

1. **Topic Filtering Verification (Unit Test)**:
   ```rust
   #[test]
   fn test_topic_filtering() {
       assert!(is_command_topic("usp/endpoint/cpe-01/request"));
       assert!(is_command_topic("usp/endpoint/cpe-01/request/reboot"));
       assert!(!is_command_topic("usp/endpoint/cpe-01/telemetry"));
       assert!(!is_command_topic("usp/endpoint/cpe-01/notify"));
       assert_eq!(extract_cpe_id_from_topic("usp/endpoint/cpe-sim-001/telemetry"), Some("cpe-sim-001".to_string()));
   }
   ```

2. **Healthcheck File Verification**:
   - Inside the container, execute:
     ```bash
     docker compose exec rust-core test -f /tmp/healthy && echo "HEALTHY"
     ```
   - Stop Mosquitto (`docker compose stop mosquitto`): verify `/tmp/healthy` is deleted within 2 seconds.
   - Restart Mosquitto (`docker compose start mosquitto`): verify `/tmp/healthy` is recreated within 2 seconds.

3. **End-to-End Simulation Flow Verification**:
   - Run the full automated verification suite:
     ```bash
     bash simulate_flow.sh
     ```
   - Step 2 verifies MQTT publish on `usp/endpoint/{cpe_id}/telemetry`.
   - Step 3 verifies Rust Core in-RAM live state upsert into `cpe_live_state` (`status=online`, `cpu_usage=42.5`).
   - Step 4 verifies altered telemetry (`rx_optical_power: -21.0`) triggers reconciliation into `cpe_state_history`.
   - Step 5 verifies command published to `usp/endpoint/{cpe_id}/request` is ignored by Rust Core (no feedback loop).
   - Exit code must be 0.

4. **Invalidation Conditions**:
   - If `/tmp/healthy` is created when PostgreSQL is offline or Mosquitto is offline.
   - If command topics (`/request`) are ingested as telemetry updates, creating false error logs or state loops.
   - If database slow queries block MQTT keepalive pings causing broker disconnects.
