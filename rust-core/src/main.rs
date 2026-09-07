use anyhow::{anyhow, Context, Result};
use chrono::{DateTime, Utc};
use prost::Message;
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};
use sqlx::postgres::{PgPool, PgPoolOptions};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::fs;
use tokio::sync::mpsc::{channel, Receiver, Sender};
use tokio::sync::watch;

// Include Protobuf generated definitions compiled by prost_build in build.rs
pub mod usp {
    include!(concat!(env!("OUT_DIR"), "/usp.rs"));
}

// -----------------------------------------------------------------------------
// Domain Models
// -----------------------------------------------------------------------------

/// Internal decoupled telemetry update payload transferred across MPSC channel
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

/// JSON payload structure matching simulate_flow.sh format
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonTelemetryPayload {
    pub cpe_id: Option<String>,
    pub endpoint_id: Option<String>,
    pub status: Option<String>,
    #[serde(default)]
    pub metrics: Option<JsonValue>,
    #[serde(default)]
    pub telemetry_metrics: Option<JsonValue>,
    #[serde(default)]
    pub parameters: Option<JsonValue>,
    #[serde(default)]
    pub current_parameters: Option<JsonValue>,
    pub ip_address: Option<String>,
    pub firmware_version: Option<String>,
}

// -----------------------------------------------------------------------------
// Topic Helpers
// -----------------------------------------------------------------------------

/// Evaluates if topic is a command request from Controller that must be ignored
pub fn is_command_topic(topic: &str) -> bool {
    topic.ends_with("/request") || topic.contains("/request/")
}

/// Extracts CPE identifier from topic path: usp/endpoint/{cpe_id}/...
pub fn extract_cpe_id_from_topic(topic: &str) -> Option<String> {
    let parts: Vec<&str> = topic.split('/').collect();
    if parts.len() >= 3 && parts[0] == "usp" && parts[1] == "endpoint" {
        let candidate = parts[2].trim();
        if !candidate.is_empty() {
            return Some(candidate.to_string());
        }
    }
    None
}

// -----------------------------------------------------------------------------
// Dual Payload Decoder
// -----------------------------------------------------------------------------

pub struct PayloadDecoder;

impl PayloadDecoder {
    /// Dual payload decoding entry point: inspects byte signature, attempts
    /// priority path, and falls back gracefully.
    pub fn decode(topic: &str, raw: &[u8]) -> Result<TelemetryUpdate> {
        if raw.is_empty() {
            return Err(anyhow!("Received empty payload"));
        }

        let fallback_cpe_id = extract_cpe_id_from_topic(topic)
            .ok_or_else(|| anyhow!("Unable to extract cpe_id from topic: {}", topic))?;

        // Inspect first non-whitespace byte
        let first_byte = raw.iter().copied().find(|b| !b.is_ascii_whitespace());

        if let Some(b'{') | Some(b'[') = first_byte {
            // Priority JSON decoding path
            match Self::decode_json(&fallback_cpe_id, raw) {
                Ok(update) => Ok(update),
                Err(json_err) => {
                    tracing::debug!("JSON decoding failed, attempting Protobuf fallback: {json_err:#}");
                    Self::decode_protobuf(&fallback_cpe_id, raw)
                        .with_context(|| format!("JSON failed ({json_err}) and Protobuf fallback failed"))
                }
            }
        } else {
            // Priority Protobuf decoding path
            match Self::decode_protobuf(&fallback_cpe_id, raw) {
                Ok(update) => Ok(update),
                Err(pb_err) => {
                    tracing::debug!("Protobuf decoding failed, attempting JSON fallback: {pb_err:#}");
                    Self::decode_json(&fallback_cpe_id, raw)
                        .with_context(|| format!("Protobuf failed ({pb_err}) and JSON fallback failed"))
                }
            }
        }
    }

    /// Decode JSON payloads (compatible with simulate_flow.sh Steps 2 & 4)
    pub fn decode_json(fallback_cpe_id: &str, raw: &[u8]) -> Result<TelemetryUpdate> {
        let payload: JsonTelemetryPayload = serde_json::from_slice(raw)
            .context("Failed to deserialize JSON telemetry payload")?;

        let cpe_id = payload
            .cpe_id
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| fallback_cpe_id.to_string());

        let endpoint_id = payload
            .endpoint_id
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .or_else(|| Some(cpe_id.clone()));

        let status = payload
            .status
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "online".to_string());

        // Consolidate metrics
        let mut telemetry_metrics = match (payload.telemetry_metrics, payload.metrics) {
            (Some(JsonValue::Object(m1)), Some(JsonValue::Object(m2))) => {
                let mut merged = m1;
                for (k, v) in m2 {
                    merged.insert(k, v);
                }
                JsonValue::Object(merged)
            }
            (Some(m), None) | (None, Some(m)) => m,
            _ => json!({}),
        };

        // Consolidate parameters
        let current_parameters = match (payload.current_parameters, payload.parameters) {
            (Some(JsonValue::Object(p1)), Some(JsonValue::Object(p2))) => {
                let mut merged = p1;
                for (k, v) in p2 {
                    merged.insert(k, v);
                }
                JsonValue::Object(merged)
            }
            (Some(p), None) | (None, Some(p)) => p,
            _ => json!({}),
        };

        // If optical power is in parameters but not in metrics, copy it over for trigger compatibility
        if let JsonValue::Object(ref mut metrics_map) = telemetry_metrics {
            if !metrics_map.contains_key("rx_optical_power") {
                if let JsonValue::Object(ref params_map) = current_parameters {
                    for (k, v) in params_map {
                        if k.contains("Optical") && (k.ends_with("OpticalSignalLevel") || k.ends_with("RxPower")) {
                            if let Some(s) = v.as_str() {
                                if let Ok(n) = s.parse::<f64>() {
                                    metrics_map.insert("rx_optical_power".to_string(), json!(n));
                                    break;
                                }
                            } else if let Some(n) = v.as_f64() {
                                metrics_map.insert("rx_optical_power".to_string(), json!(n));
                                break;
                            }
                        }
                    }
                }
            }
        }

        let mut firmware_version = payload.firmware_version;
        if firmware_version.is_none() {
            if let Some(fw) = current_parameters
                .get("Device.DeviceInfo.SoftwareVersion")
                .and_then(|v| v.as_str())
            {
                firmware_version = Some(fw.to_string());
            }
        }

        let mut ip_address = payload.ip_address;
        if ip_address.is_none() {
            if let Some(ip) = current_parameters
                .get("Device.IP.Interface.1.IPv4Address.1.IPAddress")
                .and_then(|v| v.as_str())
            {
                ip_address = Some(ip.to_string());
            }
        }

        Ok(TelemetryUpdate {
            cpe_id,
            endpoint_id,
            status,
            current_parameters,
            telemetry_metrics,
            ip_address,
            firmware_version,
            timestamp: Utc::now(),
        })
    }

    /// Decode BBF TR-369 Protobuf payload:
    /// Record -> NoSessionContextRecord -> Msg -> Body -> Request::Notify / Response::GetResp
    pub fn decode_protobuf(fallback_cpe_id: &str, raw: &[u8]) -> Result<TelemetryUpdate> {
        // Try decoding as Record first
        let (from_id, msg_bytes) = if let Ok(record) = usp::Record::decode(raw) {
            let extracted_bytes = match record.record_type {
                Some(usp::record::RecordType::NoSessionContext(no_session)) => no_session.payload,
                Some(usp::record::RecordType::SessionContext(session)) => {
                    session.payload.into_iter().flatten().collect::<Vec<u8>>()
                }
                None => Vec::new(),
            };
            (record.from_id, extracted_bytes)
        } else {
            // Fallback: raw bytes may directly be a serialized usp::Msg
            (String::new(), raw.to_vec())
        };

        let cpe_id = if !from_id.is_empty() {
            from_id
                .trim_start_matches("proto::")
                .trim_start_matches("urn:bbf:usp:id:")
                .trim()
                .to_string()
        } else {
            fallback_cpe_id.to_string()
        };

        let endpoint_id = if !from_id.is_empty() {
            Some(from_id)
        } else {
            Some(fallback_cpe_id.to_string())
        };

        let mut current_parameters = serde_json::Map::new();
        let mut telemetry_metrics = serde_json::Map::new();
        let status = "online".to_string();

        if !msg_bytes.is_empty() {
            let msg = usp::Msg::decode(&msg_bytes[..])
                .context("Failed to decode inner usp::Msg protobuf envelope")?;

            if let Some(body) = msg.body {
                match body.msg_body {
                    Some(usp::body::MsgBody::Request(req)) => {
                        if let Some(usp::request::ReqType::Notify(notify)) = req.req_type {
                            match notify.notification {
                                Some(usp::notify::Notification::Event(event)) => {
                                    for (k, v) in event.params {
                                        Self::process_param(
                                            &k,
                                            &v,
                                            &mut current_parameters,
                                            &mut telemetry_metrics,
                                        );
                                    }
                                }
                                Some(usp::notify::Notification::ValueChange(vc)) => {
                                    Self::process_param(
                                        &vc.param_path,
                                        &vc.param_value,
                                        &mut current_parameters,
                                        &mut telemetry_metrics,
                                    );
                                }
                                _ => {}
                            }
                        }
                    }
                    Some(usp::body::MsgBody::Response(resp)) => {
                        if let Some(usp::response::RespType::GetResp(get_resp)) = resp.resp_type {
                            for path_res in get_resp.req_path_results {
                                for resolved in path_res.resolved_path_results {
                                    for (k, v) in resolved.result_params {
                                        Self::process_param(
                                            &k,
                                            &v,
                                            &mut current_parameters,
                                            &mut telemetry_metrics,
                                        );
                                    }
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        }

        let ip_address = current_parameters
            .get("Device.IP.Interface.1.IPv4Address.1.IPAddress")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let firmware_version = current_parameters
            .get("Device.DeviceInfo.SoftwareVersion")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        Ok(TelemetryUpdate {
            cpe_id,
            endpoint_id,
            status,
            current_parameters: JsonValue::Object(current_parameters),
            telemetry_metrics: JsonValue::Object(telemetry_metrics),
            ip_address,
            firmware_version,
            timestamp: Utc::now(),
        })
    }

    /// Process TR-181 parameter key-value pairs into raw parameters and typed metrics
    fn process_param(
        key: &str,
        val: &str,
        params: &mut serde_json::Map<String, JsonValue>,
        metrics: &mut serde_json::Map<String, JsonValue>,
    ) {
        params.insert(key.to_string(), JsonValue::String(val.to_string()));

        // Normalization for optical trigger and metrics
        let key_lower = key.to_lowercase();
        if key_lower.contains("optical")
            && (key.ends_with("OpticalSignalLevel")
                || key.ends_with("RxPower")
                || key_lower.contains("rx_optical_power"))
        {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("rx_optical_power".to_string(), json!(num));
            }
        } else if key_lower.contains("cpu") || key.ends_with("CPUUsage") || key_lower.contains("cpu_usage") {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("cpu_usage".to_string(), json!(num));
            }
        } else if key_lower.contains("memory")
            && (key.ends_with("MemoryUsage") || key_lower.contains("memory_usage"))
        {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("memory_usage".to_string(), json!(num));
            }
        } else if key_lower.contains("temperature") {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("temperature".to_string(), json!(num));
            }
        } else if key.ends_with("BytesReceived") || key_lower.contains("rx_bytes") {
            if let Ok(num) = val.parse::<i64>() {
                metrics.insert("rx_bytes".to_string(), json!(num));
            }
        } else if key.ends_with("BytesSent") || key_lower.contains("tx_bytes") {
            if let Ok(num) = val.parse::<i64>() {
                metrics.insert("tx_bytes".to_string(), json!(num));
            }
        }
    }
}

// -----------------------------------------------------------------------------
// Health Monitoring State
// -----------------------------------------------------------------------------

#[derive(Debug, Default)]
pub struct HealthState {
    mqtt_live: AtomicBool,
    db_live: AtomicBool,
}

impl HealthState {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    pub fn set_mqtt_live(&self, live: bool) {
        self.mqtt_live.store(live, Ordering::Release);
    }

    pub fn set_db_live(&self, live: bool) {
        self.db_live.store(live, Ordering::Release);
    }

    pub fn is_healthy(&self) -> bool {
        self.mqtt_live.load(Ordering::Acquire) && self.db_live.load(Ordering::Acquire)
    }
}

/// Periodic healthcheck task touching /tmp/healthy (satisfies docker-compose.yml:65)
pub async fn run_healthcheck_monitor(
    health: Arc<HealthState>,
    db_pool: PgPool,
    mut shutdown_rx: watch::Receiver<bool>,
) {
    const HEALTH_FILE: &str = "/tmp/healthy";
    let mut interval = tokio::time::interval(Duration::from_secs(2));

    // Remove legacy health file on startup
    let _ = fs::remove_file(HEALTH_FILE).await;

    loop {
        tokio::select! {
            _ = interval.tick() => {
                let db_probe = sqlx::query("SELECT 1").execute(&db_pool).await;
                let db_ok = match db_probe {
                    Ok(_) => {
                        health.set_db_live(true);
                        true
                    }
                    Err(e) => {
                        tracing::warn!("Healthcheck PostgreSQL probe failed: {e}");
                        health.set_db_live(false);
                        false
                    }
                };

                let mqtt_ok = health.mqtt_live.load(Ordering::Acquire);

                if db_ok && mqtt_ok {
                    let now = Utc::now().timestamp();
                    let content = format!("healthy: timestamp={}\n", now);
                    if let Err(e) = fs::write(HEALTH_FILE, content).await {
                        tracing::error!("Healthcheck failed to write {}: {}", HEALTH_FILE, e);
                    }
                } else {
                    let _ = fs::remove_file(HEALTH_FILE).await;
                }
            }
            _ = shutdown_rx.changed() => {
                if *shutdown_rx.borrow() {
                    let _ = fs::remove_file(HEALTH_FILE).await;
                    break;
                }
            }
        }
    }
}

// -----------------------------------------------------------------------------
// PostgreSQL Sink
// -----------------------------------------------------------------------------

const AUTO_PROVISION_INVENTORY_SQL: &str = r#"
INSERT INTO cpe_inventory (
    cpe_id,
    serial_number,
    manufacturer,
    model,
    status
) VALUES (
    $1, $1, 'Auto-Discovered', 'Generic-USP', 'online'
)
ON CONFLICT (cpe_id) DO NOTHING;
"#;

const UPSERT_LIVE_STATE_SQL: &str = r#"
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
    $1,
    $2,
    COALESCE($3, '{}'::jsonb),
    COALESCE($4, '{}'::jsonb),
    COALESCE($5, 'online'),
    $6,
    $7,
    COALESCE($8, CURRENT_TIMESTAMP),
    CURRENT_TIMESTAMP
)
ON CONFLICT (cpe_id) DO UPDATE SET
    endpoint_id = COALESCE(EXCLUDED.endpoint_id, cpe_live_state.endpoint_id),
    current_parameters = cpe_live_state.current_parameters || COALESCE(EXCLUDED.current_parameters, '{}'::jsonb),
    telemetry_metrics = cpe_live_state.telemetry_metrics || COALESCE(EXCLUDED.telemetry_metrics, '{}'::jsonb),
    status = COALESCE(EXCLUDED.status, cpe_live_state.status),
    ip_address = COALESCE(EXCLUDED.ip_address, cpe_live_state.ip_address),
    firmware_version = COALESCE(EXCLUDED.firmware_version, cpe_live_state.firmware_version),
    last_seen = COALESCE(EXCLUDED.last_seen, cpe_live_state.last_seen),
    updated_at = CURRENT_TIMESTAMP;
"#;

pub async fn run_db_sink(
    db_pool: PgPool,
    mut rx: Receiver<TelemetryUpdate>,
    health: Arc<HealthState>,
) {
    tracing::info!("Starting Database Sink task");

    while let Some(update) = rx.recv().await {
        // 1. Auto-provision inventory row to prevent FK 23503 error on un-registered devices
        if let Err(e) = sqlx::query(AUTO_PROVISION_INVENTORY_SQL)
            .bind(&update.cpe_id)
            .execute(&db_pool)
            .await
        {
            tracing::warn!("Auto-provisioning inventory failed for {}: {}", update.cpe_id, e);
        }

        // 2. Perform atomic UPSERT into cpe_live_state with JSONB concatenation
        let mut retries = 0;
        loop {
            let res = sqlx::query(UPSERT_LIVE_STATE_SQL)
                .bind(&update.cpe_id)
                .bind(&update.endpoint_id)
                .bind(&update.current_parameters)
                .bind(&update.telemetry_metrics)
                .bind(&update.status)
                .bind(&update.ip_address)
                .bind(&update.firmware_version)
                .bind(update.timestamp)
                .execute(&db_pool)
                .await;

            match res {
                Ok(_) => {
                    health.set_db_live(true);
                    tracing::debug!(cpe_id = %update.cpe_id, "Upserted cpe_live_state successfully");
                    break;
                }
                Err(err) => {
                    retries += 1;
                    tracing::error!(
                        cpe_id = %update.cpe_id,
                        error = %err,
                        attempt = retries,
                        "Failed to upsert cpe_live_state"
                    );
                    if retries >= 3 {
                        tracing::error!(cpe_id = %update.cpe_id, "Exceeded max retries for live state upsert");
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(150 * retries)).await;
                }
            }
        }
    }

    tracing::info!("Database Sink channel drained cleanly");
}

// -----------------------------------------------------------------------------
// MQTT Ingest Task
// -----------------------------------------------------------------------------

pub async fn run_mqtt_ingest(
    mqtt_host: String,
    mqtt_port: u16,
    client_id: String,
    tx: Sender<TelemetryUpdate>,
    health: Arc<HealthState>,
    mut shutdown_rx: watch::Receiver<bool>,
) {
    tracing::info!(host = %mqtt_host, port = mqtt_port, "Starting MQTT Ingest task");

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
                    tracing::info!("MQTT ingest received shutdown signal");
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
                                tracing::info!(?connack, "Connected to Mosquitto MQTT broker");
                                health.set_mqtt_live(true);

                                if let Err(e) = client.subscribe("usp/endpoint/#", QoS::AtLeastOnce).await {
                                    tracing::error!("Failed to subscribe to usp/endpoint/#: {e}");
                                } else {
                                    tracing::info!("Subscribed to usp/endpoint/# with QoS 1");
                                }
                            }
                            Event::Incoming(Packet::Publish(publish)) => {
                                health.set_mqtt_live(true);
                                let topic = publish.topic.clone();

                                // Filter 1: Ignore command requests to prevent echo loops
                                if is_command_topic(&topic) {
                                    tracing::debug!(%topic, "Ignored command request topic");
                                    continue;
                                }

                                // Filter 2: Ignore non-endpoint topics
                                if !topic.starts_with("usp/endpoint/") {
                                    tracing::trace!(%topic, "Ignored non-endpoint topic");
                                    continue;
                                }

                                // Dual decode payload
                                match PayloadDecoder::decode(&topic, &publish.payload) {
                                    Ok(update) => {
                                        // Enqueue with timeout to prevent blocking MQTT keepalives
                                        match tokio::time::timeout(Duration::from_millis(500), tx.send(update)).await {
                                            Ok(Ok(())) => {
                                                tracing::trace!(%topic, "Enqueued telemetry update to MPSC channel");
                                            }
                                            Ok(Err(_)) => {
                                                tracing::error!("MPSC channel receiver dropped");
                                                break;
                                            }
                                            Err(_) => {
                                                tracing::warn!(%topic, "MPSC channel saturated (>500ms); dropping update to preserve MQTT keepalive");
                                            }
                                        }
                                    }
                                    Err(err) => {
                                        tracing::warn!(%topic, error = %err, "Failed to decode payload");
                                    }
                                }
                            }
                            Event::Incoming(Packet::PingResp) => {
                                health.set_mqtt_live(true);
                            }
                            Event::Incoming(Packet::Disconnect) => {
                                tracing::warn!("Disconnected from MQTT broker");
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

// -----------------------------------------------------------------------------
// Main Application Bootstrap
// -----------------------------------------------------------------------------

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "rust_core=info,info".into()),
        )
        .init();

    tracing::info!("Starting TR-369 / USP Rust Core Worker Engine");

    let database_url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgres://acs_user:acs_password@postgres:5432/acs_db".to_string());
    let mqtt_host = std::env::var("MQTT_HOST").unwrap_or_else(|_| "mosquitto".to_string());
    let mqtt_port: u16 = std::env::var("MQTT_PORT")
        .unwrap_or_else(|_| "1883".to_string())
        .parse()
        .unwrap_or(1883);
    let channel_capacity: usize = std::env::var("MPSC_CAPACITY")
        .unwrap_or_else(|_| "1024".to_string())
        .parse()
        .unwrap_or(1024);

    // Connect to PostgreSQL with retry
    let mut db_pool = None;
    let mut retry_delay = Duration::from_secs(1);
    for attempt in 1..=15 {
        tracing::info!(attempt, "Connecting to PostgreSQL database...");
        match PgPoolOptions::new()
            .max_connections(10)
            .min_connections(2)
            .acquire_timeout(Duration::from_secs(5))
            .connect(&database_url)
            .await
        {
            Ok(pool) => {
                tracing::info!("Connected to PostgreSQL successfully");
                db_pool = Some(pool);
                break;
            }
            Err(e) => {
                tracing::warn!(attempt, error = %e, "Database connection failed; retrying...");
                tokio::time::sleep(retry_delay).await;
                retry_delay = std::cmp::min(retry_delay * 2, Duration::from_secs(5));
            }
        }
    }

    let db_pool = db_pool.ok_or_else(|| anyhow!("Unable to connect to PostgreSQL after retries"))?;

    let health = HealthState::new();
    let (tx, rx) = channel::<TelemetryUpdate>(channel_capacity);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // Spawn Health Monitor Task
    let health_handle = tokio::spawn(run_healthcheck_monitor(
        health.clone(),
        db_pool.clone(),
        shutdown_rx.clone(),
    ));

    // Spawn PostgreSQL Sink Task
    let db_handle = tokio::spawn(run_db_sink(
        db_pool.clone(),
        rx,
        health.clone(),
    ));

    // Spawn MQTT Ingest Task
    let mqtt_handle = tokio::spawn(run_mqtt_ingest(
        mqtt_host,
        mqtt_port,
        "rust-usp-core".to_string(),
        tx,
        health.clone(),
        shutdown_rx.clone(),
    ));

    // Wait for termination signal
    tokio::signal::ctrl_c().await.context("Failed to listen for ctrl_c signal")?;
    tracing::info!("Received shutdown signal. Initiating graceful shutdown...");
    let _ = shutdown_tx.send(true);

    let _ = tokio::join!(mqtt_handle, db_handle, health_handle);
    tracing::info!("TR-369 Rust Core Worker shutdown complete.");

    Ok(())
}

// -----------------------------------------------------------------------------
// Unit Tests
// -----------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn test_topic_filtering_and_extraction() {
        assert!(is_command_topic("usp/endpoint/cpe-01/request"));
        assert!(is_command_topic("usp/endpoint/cpe-01/request/reboot"));
        assert!(!is_command_topic("usp/endpoint/cpe-01/notify"));
        assert!(!is_command_topic("usp/endpoint/cpe-01/telemetry"));

        assert_eq!(
            extract_cpe_id_from_topic("usp/endpoint/cpe-sim-001/telemetry"),
            Some("cpe-sim-001".to_string())
        );
        assert_eq!(
            extract_cpe_id_from_topic("usp/endpoint/cpe-sim-001/notify"),
            Some("cpe-sim-001".to_string())
        );
        assert_eq!(extract_cpe_id_from_topic("invalid/topic"), None);
    }

    #[test]
    fn test_decode_json_step2_payload() {
        let payload = r#"{
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
        }"#;

        let res = PayloadDecoder::decode("usp/endpoint/cpe-sim-001/telemetry", payload.as_bytes())
            .expect("Decoding Step 2 JSON payload must succeed");

        assert_eq!(res.cpe_id, "cpe-sim-001");
        assert_eq!(res.status, "online");
        assert_eq!(
            res.telemetry_metrics.get("cpu_usage").and_then(|v| v.as_f64()),
            Some(42.5)
        );
        assert_eq!(
            res.telemetry_metrics.get("rx_optical_power").and_then(|v| v.as_f64()),
            Some(-18.5)
        );
        assert_eq!(
            res.current_parameters.get("Device.WiFi.Radio.1.Status").and_then(|v| v.as_str()),
            Some("Up")
        );
        assert_eq!(res.firmware_version, Some("1.2.0-prod".to_string()));
    }

    #[test]
    fn test_decode_json_step4_altered_payload() {
        let payload = r#"{
            "cpe_id": "cpe-sim-001",
            "status": "online",
            "metrics": {
                "rx_optical_power": -21.0,
                "cpu_usage": 88.4,
                "memory_usage": 75.2,
                "rx_bytes": 2097152,
                "tx_bytes": 1048576,
                "temperature": 52.8
            },
            "parameters": {
                "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
                "Device.WiFi.Radio.1.Status": "Up"
            }
        }"#;

        let res = PayloadDecoder::decode("usp/endpoint/cpe-sim-001/telemetry", payload.as_bytes())
            .expect("Decoding Step 4 JSON payload must succeed");

        assert_eq!(res.cpe_id, "cpe-sim-001");
        assert_eq!(
            res.telemetry_metrics.get("cpu_usage").and_then(|v| v.as_f64()),
            Some(88.4)
        );
        assert_eq!(
            res.telemetry_metrics.get("rx_optical_power").and_then(|v| v.as_f64()),
            Some(-21.0)
        );
    }

    #[test]
    fn test_decode_protobuf_wire_compatible_record() {
        // Construct a genuine TR-369 Record carrying a Notify ValueChange message
        let msg = usp::Msg {
            header: Some(usp::Header {
                msg_id: "test-msg-001".to_string(),
                msg_type: usp::header::MsgType::Notify as i32,
            }),
            body: Some(usp::Body {
                msg_body: Some(usp::body::MsgBody::Request(usp::Request {
                    req_type: Some(usp::request::ReqType::Notify(usp::Notify {
                        subscription_id: "sub-1".to_string(),
                        send_resp: false,
                        notification: Some(usp::notify::Notification::ValueChange(
                            usp::notify::ValueChange {
                                param_path: "Device.Optical.Interface.1.OpticalSignalLevel".to_string(),
                                param_value: "-21.5".to_string(),
                            },
                        )),
                    })),
                })),
            }),
        };

        let mut msg_bytes = Vec::new();
        msg.encode(&mut msg_bytes).expect("Msg encoding must succeed");

        let record = usp::Record {
            version: "1.3".to_string(),
            to_id: "proto::controller".to_string(),
            from_id: "proto::cpe-sim-001".to_string(),
            payload_security: usp::record::PayloadSecurity::Plaintext as i32,
            mac_signature: vec![],
            sender_cert: vec![],
            record_type: Some(usp::record::RecordType::NoSessionContext(
                usp::NoSessionContextRecord {
                    payload: msg_bytes,
                },
            )),
        };

        let mut record_bytes = Vec::new();
        record.encode(&mut record_bytes).expect("Record encoding must succeed");

        // Test dual decoder on protobuf binary bytes
        let res = PayloadDecoder::decode("usp/endpoint/cpe-sim-001/notify", &record_bytes)
            .expect("Decoding TR-369 Protobuf record must succeed");

        assert_eq!(res.cpe_id, "cpe-sim-001");
        assert_eq!(res.status, "online");
        assert_eq!(
            res.current_parameters
                .get("Device.Optical.Interface.1.OpticalSignalLevel")
                .and_then(|v| v.as_str()),
            Some("-21.5")
        );
        assert_eq!(
            res.telemetry_metrics
                .get("rx_optical_power")
                .and_then(|v| v.as_f64()),
            Some(-21.5)
        );
    }

    #[test]
    fn test_decode_protobuf_event_notification() {
        let mut params = HashMap::new();
        params.insert("Device.Optical.Interface.1.OpticalSignalLevel".to_string(), "-19.8".to_string());
        params.insert("Device.DeviceInfo.SoftwareVersion".to_string(), "2.0.1".to_string());
        params.insert("Device.IP.Interface.1.IPv4Address.1.IPAddress".to_string(), "192.168.1.1".to_string());
        params.insert("Device.CPU.CPUUsage".to_string(), "35.0".to_string());

        let msg = usp::Msg {
            header: Some(usp::Header {
                msg_id: "event-msg-002".to_string(),
                msg_type: usp::header::MsgType::Notify as i32,
            }),
            body: Some(usp::Body {
                msg_body: Some(usp::body::MsgBody::Request(usp::Request {
                    req_type: Some(usp::request::ReqType::Notify(usp::Notify {
                        subscription_id: "sub-event-1".to_string(),
                        send_resp: false,
                        notification: Some(usp::notify::Notification::Event(
                            usp::notify::Event {
                                obj_path: "Device.LocalAgent.".to_string(),
                                event_name: "PeriodicTelemetry!".to_string(),
                                params,
                            },
                        )),
                    })),
                })),
            }),
        };

        let mut msg_bytes = Vec::new();
        msg.encode(&mut msg_bytes).expect("Msg encoding must succeed");

        let record = usp::Record {
            version: "1.3".to_string(),
            to_id: "proto::controller".to_string(),
            from_id: "urn:bbf:usp:id:router-42".to_string(),
            payload_security: usp::record::PayloadSecurity::Plaintext as i32,
            mac_signature: vec![],
            sender_cert: vec![],
            record_type: Some(usp::record::RecordType::NoSessionContext(
                usp::NoSessionContextRecord {
                    payload: msg_bytes,
                },
            )),
        };

        let mut record_bytes = Vec::new();
        record.encode(&mut record_bytes).expect("Record encoding must succeed");

        let res = PayloadDecoder::decode("usp/endpoint/router-42/notify", &record_bytes)
            .expect("Decoding Protobuf Event must succeed");

        assert_eq!(res.cpe_id, "router-42");
        assert_eq!(res.status, "online");
        assert_eq!(
            res.telemetry_metrics.get("rx_optical_power").and_then(|v| v.as_f64()),
            Some(-19.8)
        );
        assert_eq!(
            res.telemetry_metrics.get("cpu_usage").and_then(|v| v.as_f64()),
            Some(35.0)
        );
        assert_eq!(res.firmware_version, Some("2.0.1".to_string()));
        assert_eq!(res.ip_address, Some("192.168.1.1".to_string()));
    }

    // =========================================================================
    // Adversarial Challenge Tests (Milestone 3 - challenger_m3_1)
    // =========================================================================

    #[test]
    fn test_adversarial_empty_payload() {
        let res = PayloadDecoder::decode("usp/endpoint/cpe1/notify", &[]);
        assert!(res.is_err(), "Empty payload must return an Err, not panic");
        let err_msg = res.unwrap_err().to_string();
        assert!(err_msg.contains("Received empty payload"));
    }

    #[test]
    fn test_adversarial_whitespace_payload() {
        let whitespace = b"   \t\r\n  \n\t ";
        let res = PayloadDecoder::decode("usp/endpoint/cpe1/notify", whitespace);
        assert!(res.is_err(), "Whitespace-only payload must return an Err, not panic");
    }

    #[test]
    fn test_adversarial_malformed_json_syntax() {
        // Truncated object
        let res = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"{\"cpe_id\": \"cpe1\", \"metrics\": {");
        assert!(res.is_err(), "Truncated JSON must return Err");

        // Truncated array
        let res2 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"[\"cpe1\", 123");
        assert!(res2.is_err(), "Truncated JSON array must return Err");

        // Invalid JSON trailing comma
        let res3 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"{\"cpe_id\": \"cpe1\",}");
        assert!(res3.is_err(), "Trailing comma JSON must return Err");

        // Malformed literal
        let res4 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"{\"status\": unquoted_value}");
        assert!(res4.is_err(), "Unquoted literal JSON must return Err");
    }

    #[test]
    fn test_adversarial_json_wrong_types() {
        // cpe_id is a number instead of string
        let res1 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"{\"cpe_id\": 12345}");
        assert!(res1.is_err(), "Numeric cpe_id must fail deserialization cleanly");

        // status is a boolean instead of string
        let res2 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"{\"status\": true}");
        assert!(res2.is_err(), "Boolean status must fail deserialization cleanly");

        // ip_address is an array instead of string
        let res3 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", b"{\"ip_address\": [192, 168, 1, 1]}");
        assert!(res3.is_err(), "Array ip_address must fail deserialization cleanly");
    }

    #[test]
    fn test_adversarial_non_utf8_binary_payload() {
        // Raw non-UTF8 arbitrary bytes
        let bad_bytes = [0xFF, 0xFE, 0xFD, 0x80, 0x81, 0x00, 0xC0, 0xAF, 0x90];
        let res = PayloadDecoder::decode("usp/endpoint/cpe1/notify", &bad_bytes);
        assert!(res.is_err(), "Non-UTF8 binary payload must return Err without panicking");

        // Non-UTF8 bytes embedded inside JSON delimiter
        let mut bad_json = b"{\"cpe_id\": \"".to_vec();
        bad_json.extend_from_slice(&[0xFF, 0xFE, 0x80]);
        bad_json.extend_from_slice(b"\"}");
        let res2 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", &bad_json);
        assert!(res2.is_err(), "Non-UTF8 JSON payload must return Err without panicking");
    }

    #[test]
    fn test_adversarial_invalid_protobuf_bytes() {
        // Varint with continuation bit set but abruptly truncated
        let truncated_varint = [0x08, 0x80];
        let res1 = PayloadDecoder::decode("usp/endpoint/cpe1/notify", &truncated_varint);
        assert!(res1.is_err(), "Truncated varint must return Err");

        // Length-delimited field with length exceeding buffer
        let truncated_string = [0x0A, 0x64, 0x01, 0x02];
        let res2 = PayloadDecoder::decode("usp/endpoint/cpe1/notify", &truncated_string);
        assert!(res2.is_err(), "Truncated length-delimited protobuf must return Err");

        // Deprecated / invalid wire type (wire type 6 or 7)
        let invalid_wire_type = [0x0F, 0x01];
        let res3 = PayloadDecoder::decode("usp/endpoint/cpe1/notify", &invalid_wire_type);
        assert!(res3.is_err(), "Invalid wire type must return Err");
    }

    #[test]
    fn test_adversarial_protobuf_corrupt_inner_msg() {
        let record = usp::Record {
            version: "1.3".to_string(),
            to_id: "proto::controller".to_string(),
            from_id: "proto::cpe-bad-inner".to_string(),
            payload_security: usp::record::PayloadSecurity::Plaintext as i32,
            mac_signature: vec![],
            sender_cert: vec![],
            record_type: Some(usp::record::RecordType::NoSessionContext(
                usp::NoSessionContextRecord {
                    payload: vec![0xDE, 0xAD, 0xBE, 0xEF, 0xFF, 0x00],
                },
            )),
        };
        let mut bytes = Vec::new();
        record.encode(&mut bytes).expect("Record encode should succeed");

        let res = PayloadDecoder::decode("usp/endpoint/cpe-bad-inner/notify", &bytes);
        assert!(res.is_err(), "Corrupted inner Msg must fail decoding cleanly");
        let err_str = format!("{:#}", res.unwrap_err());
        assert!(err_str.contains("Failed to decode inner usp::Msg protobuf envelope"));
    }

    #[test]
    fn test_adversarial_protobuf_empty_record_type() {
        let record = usp::Record {
            version: "1.3".to_string(),
            to_id: "proto::controller".to_string(),
            from_id: "proto::cpe-empty-type".to_string(),
            payload_security: usp::record::PayloadSecurity::Plaintext as i32,
            mac_signature: vec![],
            sender_cert: vec![],
            record_type: None,
        };
        let mut bytes = Vec::new();
        record.encode(&mut bytes).expect("Record encode should succeed");

        let res = PayloadDecoder::decode("usp/endpoint/cpe-empty-type/notify", &bytes);
        assert!(res.is_ok(), "Record with empty record_type must decode safely without panic");
        let update = res.unwrap();
        assert_eq!(update.cpe_id, "cpe-empty-type");
        assert_eq!(update.current_parameters, json!({}));
        assert_eq!(update.telemetry_metrics, json!({}));
    }

    #[test]
    fn test_adversarial_protobuf_missing_from_id_fallback() {
        let record = usp::Record {
            version: "1.3".to_string(),
            to_id: "proto::controller".to_string(),
            from_id: "".to_string(), // Missing from_id
            payload_security: usp::record::PayloadSecurity::Plaintext as i32,
            mac_signature: vec![],
            sender_cert: vec![],
            record_type: None,
        };
        let mut bytes = Vec::new();
        record.encode(&mut bytes).expect("Record encode should succeed");

        let res = PayloadDecoder::decode("usp/endpoint/cpe-fallback-topic/notify", &bytes);
        assert!(res.is_ok(), "Record without from_id must fall back to topic cpe_id");
        let update = res.unwrap();
        assert_eq!(update.cpe_id, "cpe-fallback-topic");
    }

    #[test]
    fn test_adversarial_topic_filtering_mandate() {
        // Requirement 2:
        // is_command_topic must correctly filter:
        // - usp/endpoint/cpe1/request
        // - usp/endpoint/cpe1/request/sub
        // while allowing:
        // - usp/endpoint/cpe1/notify
        // - usp/endpoint/cpe1/telemetry
        assert!(is_command_topic("usp/endpoint/cpe1/request"), "Must filter .../request");
        assert!(is_command_topic("usp/endpoint/cpe1/request/sub"), "Must filter .../request/sub");
        assert!(is_command_topic("usp/endpoint/cpe1/request/"), "Must filter .../request/");
        assert!(is_command_topic("usp/endpoint/cpe1/request/reboot/uuid-123"), "Must filter deep request");

        assert!(!is_command_topic("usp/endpoint/cpe1/notify"), "Must allow .../notify");
        assert!(!is_command_topic("usp/endpoint/cpe1/telemetry"), "Must allow .../telemetry");
        assert!(!is_command_topic("usp/endpoint/cpe1/status"), "Must allow .../status");
        assert!(!is_command_topic("usp/endpoint/cpe1/event"), "Must allow .../event");
        assert!(!is_command_topic("usp/endpoint/cpe1/request_telemetry"), "Must allow request_telemetry");
        assert!(!is_command_topic("usp/endpoint/cpe1/notify_request"), "Must allow notify_request");
    }

    #[test]
    fn test_adversarial_topic_filtering_edge_cases() {
        // Critical finding / edge case:
        // If device cpe_id is literally named "request", its notify topic contains "/request/"!
        let device_named_request_topic = "usp/endpoint/request/notify";
        let is_filtered = is_command_topic(device_named_request_topic);
        // Documents the design behavior: topic.contains("/request/") matches when cpe_id == "request"
        assert!(is_filtered, "Demonstrates that cpe_id named 'request' matches /request/ filter");

        // Topics outside usp/endpoint/
        assert!(!is_command_topic("usp/endpoint"));
        assert!(!is_command_topic("usp/controller/cpe1/notify"));
    }

    #[test]
    fn test_adversarial_cpe_id_extraction_boundaries() {
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint/cpe-007/telemetry"), Some("cpe-007".to_string()));
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint/  cpe-spaces  /telemetry"), Some("cpe-spaces".to_string()));
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint/cpe-007"), Some("cpe-007".to_string()));
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint/cpe-007/a/b/c"), Some("cpe-007".to_string()));

        // Invalid / Boundary formats
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint//telemetry"), None);
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint/   /telemetry"), None);
        assert_eq!(extract_cpe_id_from_topic("usp/endpoint"), None);
        assert_eq!(extract_cpe_id_from_topic("usp/other/cpe-007/telemetry"), None);
        assert_eq!(extract_cpe_id_from_topic("random/topic/string"), None);
        assert_eq!(extract_cpe_id_from_topic(""), None);

        // PayloadDecoder fails when cpe_id extraction fails and payload has no cpe_id
        let res = PayloadDecoder::decode("invalid/topic", b"{\"status\": \"online\"}");
        assert!(res.is_err());
        assert!(res.unwrap_err().to_string().contains("Unable to extract cpe_id from topic"));
    }

    #[test]
    fn test_adversarial_optical_power_parsing_and_nan() {
        // String optical power in parameters correctly parsed into rx_optical_power
        let p_str = r#"{"parameters": {"Device.Optical.Interface.1.OpticalSignalLevel": "-19.25"}}"#;
        let u1 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", p_str.as_bytes()).unwrap();
        assert_eq!(u1.telemetry_metrics.get("rx_optical_power").and_then(|v| v.as_f64()), Some(-19.25));

        // Numeric optical power in parameters
        let p_num = r#"{"parameters": {"Device.Optical.Interface.1.OpticalSignalLevel": -22.10}}"#;
        let u2 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", p_num.as_bytes()).unwrap();
        assert_eq!(u2.telemetry_metrics.get("rx_optical_power").and_then(|v| v.as_f64()), Some(-22.10));

        // Non-numeric optical power string ("N/A") must not crash or insert invalid float
        let p_na = r#"{"parameters": {"Device.Optical.Interface.1.OpticalSignalLevel": "N/A"}}"#;
        let u3 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", p_na.as_bytes()).unwrap();
        assert!(u3.telemetry_metrics.get("rx_optical_power").is_none());

        // "NaN" optical power string: in serde_json json!(f64::NAN) serializes to Null without panic
        let p_nan = r#"{"parameters": {"Device.Optical.Interface.1.OpticalSignalLevel": "NaN"}}"#;
        let u4 = PayloadDecoder::decode("usp/endpoint/cpe1/telemetry", p_nan.as_bytes()).unwrap();
        assert!(u4.telemetry_metrics.get("rx_optical_power").unwrap().is_null());
    }

    #[test]
    fn test_adversarial_large_payload_stress() {
        let mut large_json = String::from(r#"{"cpe_id":"cpe-stress-01","metrics":{"cpu_usage":15.5},"parameters":{"#);
        for i in 0..3000 {
            if i > 0 {
                large_json.push(',');
            }
            large_json.push_str(&format!("\"Device.Param.{}\":\"value_{}\"", i, i));
        }
        large_json.push_str("}}");

        let res = PayloadDecoder::decode("usp/endpoint/cpe-stress-01/telemetry", large_json.as_bytes());
        assert!(res.is_ok(), "3000-parameter large JSON payload must decode cleanly");
        let u = res.unwrap();
        assert_eq!(u.cpe_id, "cpe-stress-01");
        assert_eq!(u.current_parameters.as_object().unwrap().len(), 3000);
    }
}
