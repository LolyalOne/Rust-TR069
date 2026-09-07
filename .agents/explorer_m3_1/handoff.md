# Milestone 3: Protobuf & Payload Decoding Architecture Specification Report

**Author:** explorer_m3_1 (teamwork_preview_explorer)  
**Date:** 2026-09-07  
**Working Directory:** `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m3_1`  
**Milestone:** Milestone 3 — `rust-core/` Protobuf & Payload Decoding Architecture  

---

## 1. Observation

Direct observations from codebase inspection, specifications, schema files, and network probes:

1. **User Request & Architectural Requirements (`ORIGINAL_REQUEST.md`)**:
   - Lines 19–20 (R1): Memory limits: Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB), Python FastAPI (1 GB).
   - Lines 31–33 (R5): *"Serviço assíncrono (tokio, rumqttc, sqlx) processando mensagens do broker (`usp/endpoint/#`) e gravando estados no banco de dados usando MPSC Channels para desacoplamento. Para a decodificação de payloads via Protobuf (`prost`), a equipe deve baixar os arquivos `.proto` oficiais diretamente do repositório da Broadband Forum (BBF) ou criar um `.proto` de mock mínimo que simule o padrão USP."*
   - Lines 48–54: Verification flow requiring automated CLI simulation (`simulate_flow.sh`):
     - Step 1: Register test CPE via FastAPI (`POST /api/v1/cpes`).
     - Step 2: Publish TR-369 telemetry via MQTT (Mosquitto).
     - Step 3: Validate Rust worker updates RAM table (`cpe_live_state`).
     - Step 4: Validate reconciliation trigger persists metrics to historical table (`cpe_state_history`).
     - Step 5: Dispatch command via API that publishes request back to MQTT broker.

2. **Project Blueprint & Interface Contracts (`.agents/orchestrator_2/PROJECT.md`)**:
   - Lines 11–12: *"Rust USP Core Worker: High-performance consumer using `tokio`, `rumqttc`, `sqlx`, and `prost` with internal MPSC channel decoupling MQTT ingest from PostgreSQL batch upserting. Decodes TR-369 Protobuf Records and Msg envelopes from `usp/endpoint/#`."*
   - Lines 29–32: Features 11–14 detailing TR-369 Protobuf Schema, Rust MPSC Pipeline, Rust TR-369 Ingest, and Rust Core Multi-Stage Dockerfile.
   - Line 54: Inbound payload contract: *"Binary Protobuf TR-369 `Record` carrying serialized `Msg` (with JSON fallback for test simulation)."*

3. **End-to-End Simulation Script (`simulate_flow.sh`) Payload Contracts**:
   - Lines 511–529 (Step 2 - Telemetry Payload):
     ```json
     {
       "cpe_id": "${CPE_ID}",
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
   - Lines 531–537: Topics used:
     - `TELEMETRY_TOPIC="usp/endpoint/${CPE_ID}/telemetry"`
     - `NOTIFY_TOPIC="usp/endpoint/${CPE_ID}/notify"`
   - Lines 554–566 (Step 3 Assertions):
     - Checks `status == "online"`
     - Checks `telemetry_metrics.cpu_usage == "42.5"` or `metrics.cpu_usage == "42.5"`
   - Lines 595–613 (Step 4 - Altered Telemetry Payload):
     - `rx_optical_power`: changed from `-18.5` to `-21.0` (delta = 2.5 dBm > 1.0 dBm)
     - `cpu_usage`: changed from `42.5` to `88.4`
     - Step 4 asserts `cpe_state_history` count >= 2.

4. **PostgreSQL Hybrid Schema & Trigger Logic (`postgres/init.sql`)**:
   - Lines 50–60: `cpe_live_state` unlogged table in `ram_tablespace`:
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
   - Lines 135–150: Optical power extraction in `reconcile_live_to_history()` trigger:
     ```sql
     IF NEW.telemetry_metrics IS NOT NULL THEN
         v_new_val := COALESCE(
             NEW.telemetry_metrics->>'rx_optical_power',
             NEW.telemetry_metrics->>'optical_power',
             NEW.telemetry_metrics->>'optical_rx_power',
             NEW.telemetry_metrics->>'rx_power'
         );
     END IF;
     IF v_new_val IS NULL AND NEW.current_parameters IS NOT NULL THEN
         v_new_val := COALESCE(
             NEW.current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel',
             NEW.current_parameters->>'Device.Optical.Interface.1.RxPower'
         );
     END IF;
     ```
   - Lines 189–196: Reconciles strictly when `abs(v_new_rx_power - v_old_rx_power) > 1.0`.

5. **Official Broadband Forum (BBF) Specification Probing**:
   - Probed `https://usp.technology/specification/usp-record-1-3.proto` and `usp-msg-1-3.proto`:
     - `usp_record.Record`: `version = 1`, `to_id = 2`, `from_id = 3`, `payload_security = 4`, `no_session_context = 7`, `session_context = 8`.
     - `usp_record.NoSessionContextRecord`: `bytes payload = 2` (contains serialized `usp.Msg`).
     - `usp.Msg`: `Header header = 1`, `Body body = 2`.
     - `usp.Header`: `string msg_id = 1`, `MsgType msg_type = 2`.
     - `usp.Body`: `Request request = 1`, `Response response = 2`, `Error error = 3`.
     - `usp.Request`: `Get get = 1`, `Set set = 4`, `Operate operate = 7`, `Notify notify = 8`.
     - `usp.Notify`: `subscription_id = 1`, `send_resp = 2`, `Event event = 3`, `ValueChange value_change = 4`.
     - `usp.Operate`: `command = 1`, `command_key = 2`, `send_resp = 3`, `input_args = 4`.

---

## 2. Logic Chain

1. **Standard Wire Compatibility vs. Monolithic File Convenience**:
   - *Observation Ref: Section 1.5*: Protobuf wire format is defined strictly by field number tags and wire types, independent of file boundaries or language-specific package names.
   - *Deduction*: Splitting into separate `.proto` files with `import` statements introduces fragile relative pathing in `prost_build` and `protoc`. A consolidated, self-contained `proto/usp.proto` under package `usp;` preserving exact BBF field tags achieves 100% binary wire interoperability with official BBF TR-369 implementations (such as `obuspa`) while eliminating compilation fragility and external file dependencies.

2. **Dual Payload Decoding Necessity**:
   - *Observation Ref: Section 1.1, 1.2, 1.3*: Production TR-369 CPEs transmit binary Protobuf `Record` envelopes on `usp/endpoint/{cpe_id}/notify`. However, the project's automated acceptance harness (`simulate_flow.sh`) publishes JSON payloads directly on `usp/endpoint/{cpe_id}/telemetry` and `usp/endpoint/{cpe_id}/notify`.
   - *Deduction*: If `rust-core` only implemented Protobuf decoding, `simulate_flow.sh` Step 3 and Step 4 would fail. If it only implemented JSON, it would violate TR-369 specification R5. A resilient dual payload decoding strategy with automatic format discrimination is required.

3. **Format Discrimination Strategy**:
   - *Observation Ref: Section 1.3, 1.5*: JSON payloads universally begin with `{` (0x7B) or `[` (0x5B) after optional ASCII whitespace. Valid Protobuf `Record` messages begin with field tags (e.g. tag 1 version `0x0A`, tag 2 to_id `0x12`, tag 3 from_id `0x1A`), none of which match `{` or `[`.
   - *Deduction*: An inspection of the first non-whitespace byte allows zero-cost routing:
     - If first byte is `{` or `[`: Execute JSON fast-path (`serde_json::from_slice`). If that fails, fallback to Protobuf `Record::decode`.
     - Otherwise: Execute Protobuf fast-path (`Record::decode`). If that fails, fallback to JSON.
     - If both fail, log a diagnostic warning and discard without terminating the Tokio task.

4. **Telemetry Metric Normalization for Database Alignment**:
   - *Observation Ref: Section 1.3, 1.4*: In `postgres/init.sql`, the optical trigger inspects both `telemetry_metrics->>'rx_optical_power'` and `current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel'`. Furthermore, `simulate_flow.sh` Step 3 checks `telemetry_metrics.cpu_usage == 42.5`.
   - *Deduction*: The decoding pipeline must produce a unified `TelemetryUpdate` struct where:
     - Canonical TR-181 paths are retained in `current_parameters`.
     - Standard metrics (`rx_optical_power`, `cpu_usage`, `memory_usage`, `temperature`, `rx_bytes`, `tx_bytes`) are extracted and normalized into `telemetry_metrics`.
     - This guarantees that both Protobuf notifications and JSON simulation payloads seamlessly populate `cpe_live_state` and trigger `reconcile_live_to_history` when optical variation exceeds 1.0 dBm.

5. **Topic Filtering & Loop Prevention**:
   - *Observation Ref: Section 1.1, 1.3*: Controller commands (e.g. Reboot in Step 5) are published to `usp/endpoint/{cpe_id}/request`. The worker subscribes to wildcard `usp/endpoint/#`.
   - *Deduction*: Ingestion MUST ignore any topic ending with `/request` to avoid parsing controller commands as device telemetry or triggering unintended ingestion loops.

6. **Decoupled Architecture with Tokio MPSC**:
   - *Observation Ref: Section 1.1, 1.2*: Database I/O spikes must not delay MQTT packet acknowledgments or keep-alive pings (`rumqttc`).
   - *Deduction*: A bounded `tokio::sync::mpsc::channel(1024)` bridges the MQTT ingest loop and the PostgreSQL writer task, providing backpressure and keeping memory well within the 500 MB container budget.

---

## 3. Detailed Architectural Specifications

### 3.1 `rust-core/Cargo.toml`
The crate dependencies and build configuration are structured as follows:

```toml
[package]
name = "rust-core"
version = "0.1.0"
edition = "2021"
authors = ["Teamwork ACS Engineering Team"]
description = "High-Performance Asynchronous TR-369 / USP Core Worker over MQTT"

[dependencies]
# Asynchronous Runtime & Concurrency
tokio = { version = "1.38", features = ["full"] }

# MQTT Message Transfer Protocol (MTP) Client
rumqttc = { version = "0.24", features = ["default"] }

# PostgreSQL Database Access (Pure Rust TLS for Alpine compatibility)
sqlx = { version = "0.7", features = ["runtime-tokio-rustls", "postgres", "json", "chrono", "uuid"] }

# Protobuf Serialization & Deserialization
prost = "0.12"
prost-types = "0.12"

# JSON Serialization & Manipulation
serde = { version = "1.0", features = ["derive"] }
serde_json = { version = "1.0", features = ["raw_value"] }

# Date & Time Handling
chrono = { version = "0.4", features = ["serde", "clock"] }

# Structured Tracing & Observability
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter", "fmt", "json"] }

# Utility Crates
dotenvy = "0.15"
thiserror = "1.0"
uuid = { version = "1.8", features = ["v4", "serde"] }

[build-dependencies]
prost-build = "0.12"

[profile.release]
opt-level = 3
lto = true
codegen-units = 1
panic = "abort"
strip = true
```

---

### 3.2 `rust-core/proto/usp.proto`
Self-contained, 100% wire-compatible with Broadband Forum TR-369 1.3:

```protobuf
syntax = "proto3";

package usp;

// =========================================================================
// TR-369 USP Record (Envelope) - BBF Wire-Compatible (Tag-Matched)
// =========================================================================

message Record {
  string version = 1;
  string to_id = 2;
  string from_id = 3;
  PayloadSecurity payload_security = 4;
  bytes mac_signature = 5;
  bytes sender_cert = 6;

  oneof record_type {
    NoSessionContextRecord no_session_context = 7;
    SessionContextRecord session_context = 8;
  }

  enum PayloadSecurity {
    PLAINTEXT = 0;
    TLS12 = 1;
  }
}

message NoSessionContextRecord {
  bytes payload = 2; // Serialized usp.Msg
}

message SessionContextRecord {
  uint64 session_id = 1;
  uint64 sequence_id = 2;
  uint64 expected_id = 3;
  uint64 retransmit_id = 4;
  PayloadSARState payload_sar_state = 5;
  PayloadSARState payloadrec_sar_state = 6;
  repeated bytes payload = 7;

  enum PayloadSARState {
    NONE = 0;
    BEGIN = 1;
    INPROCESS = 2;
    COMPLETE = 3;
  }
}

// =========================================================================
// TR-369 USP Msg (Application Body) - BBF Wire-Compatible
// =========================================================================

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
    ADD = 8;
    ADD_RESP = 9;
    DELETE = 10;
    DELETE_RESP = 11;
    GET_SUPPORTED_DM = 12;
    GET_SUPPORTED_DM_RESP = 13;
    GET_INSTANCES = 14;
    GET_INSTANCES_RESP = 15;
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
  repeated ParamError param_errs = 3;

  message ParamError {
    string param_path = 1;
    fixed32 err_code = 2;
    string err_msg = 3;
  }
}

// =========================================================================
// Operations, Telemetry Notifications, and Commands
// =========================================================================

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
    string event_name = 2;     // e.g. "PeriodicTelemetry!", "Boot!"
    map<string, string> params = 3;
  }

  message ValueChange {
    string param_path = 1;     // e.g. "Device.Optical.Interface.1.OpticalSignalLevel"
    string param_value = 2;    // e.g. "-18.5"
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

---

### 3.3 `rust-core/build.rs`
Build script utilizing `prost-build` with strict cache invalidation:

```rust
fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Invalidate build cache strictly when usp.proto changes
    println!("cargo:rerun-if-changed=proto/usp.proto");

    let proto_files = ["proto/usp.proto"];
    let proto_includes = ["proto"];

    let mut config = prost_build::Config::new();
    config.compile_protos(&proto_files, &proto_includes)?;

    Ok(())
}
```

---

### 3.4 Dual Payload Decoding Architecture

#### Canonical Telemetry Domain Model (`src/model.rs`):
```rust
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JsonTelemetry {
    pub cpe_id: Option<String>,
    pub status: Option<String>,
    #[serde(default)]
    pub metrics: Option<serde_json::Value>,
    #[serde(default)]
    pub telemetry_metrics: Option<serde_json::Value>,
    #[serde(default)]
    pub parameters: Option<serde_json::Value>,
    #[serde(default)]
    pub current_parameters: Option<serde_json::Value>,
    pub ip_address: Option<String>,
    pub firmware_version: Option<String>,
}

#[derive(Debug, Clone)]
pub struct TelemetryUpdate {
    pub cpe_id: String,
    pub endpoint_id: Option<String>,
    pub status: String,
    pub current_parameters: serde_json::Value,
    pub telemetry_metrics: serde_json::Value,
    pub ip_address: Option<String>,
    pub firmware_version: Option<String>,
    pub timestamp: DateTime<Utc>,
}
```

#### Dual Decoder Engine (`src/decoder.rs`):
```rust
use prost::Message;
use crate::model::{JsonTelemetry, TelemetryUpdate};
use crate::proto::usp::*;
use std::collections::HashMap;

#[derive(Debug, thiserror::Error)]
pub enum DecoderError {
    #[error("Protobuf decode error: {0}")]
    Protobuf(#[from] prost::DecodeError),
    #[error("JSON decode error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("Invalid topic format: {0}")]
    InvalidTopic(String),
    #[error("Empty payload received")]
    EmptyPayload,
    #[error("Ignored topic: {0}")]
    IgnoredTopic(String),
}

pub struct PayloadDecoder;

impl PayloadDecoder {
    /// Extract cpe_id from topic hierarchy: usp/endpoint/{cpe_id}/...
    pub fn extract_cpe_id(topic: &str) -> Result<String, DecoderError> {
        let parts: Vec<&str> = topic.split('/').collect();
        if parts.len() >= 4 && parts[3] == "request" {
            return Err(DecoderError::IgnoredTopic(topic.to_string()));
        }
        if parts.len() >= 3 && parts[0] == "usp" && parts[1] == "endpoint" {
            return Ok(parts[2].to_string());
        }
        Err(DecoderError::InvalidTopic(topic.to_string()))
    }

    /// Dual decoding entry point: inspects byte signature, attempts priority path with fallback
    pub fn decode(topic: &str, raw: &[u8]) -> Result<TelemetryUpdate, DecoderError> {
        if raw.is_empty() {
            return Err(DecoderError::EmptyPayload);
        }

        let fallback_cpe_id = Self::extract_cpe_id(topic)?;

        // Find first non-whitespace byte
        let trimmed = match raw.iter().position(|&b| !b.is_ascii_whitespace()) {
            Some(pos) => &raw[pos..],
            None => raw,
        };

        if trimmed.starts_with(b"{") || trimmed.starts_with(b"[") {
            // Fast-path JSON
            match Self::decode_json(&fallback_cpe_id, raw) {
                Ok(update) => Ok(update),
                Err(json_err) => {
                    tracing::debug!("JSON decoding failed, attempting Protobuf fallback: {json_err}");
                    Self::decode_protobuf(&fallback_cpe_id, raw).map_err(|_| DecoderError::Json(json_err))
                }
            }
        } else {
            // Fast-path Protobuf
            match Self::decode_protobuf(&fallback_cpe_id, raw) {
                Ok(update) => Ok(update),
                Err(pb_err) => {
                    tracing::debug!("Protobuf decoding failed, attempting JSON fallback: {pb_err}");
                    Self::decode_json(&fallback_cpe_id, raw).map_err(|_| DecoderError::Protobuf(pb_err))
                }
            }
        }
    }

    /// Decode JSON simulation payloads (simulate_flow.sh Step 2 & Step 4)
    fn decode_json(fallback_cpe_id: &str, raw: &[u8]) -> Result<TelemetryUpdate, serde_json::Error> {
        let payload: JsonTelemetry = serde_json::from_slice(raw)?;

        let cpe_id = payload.cpe_id.unwrap_or_else(|| fallback_cpe_id.to_string());
        let status = payload.status.unwrap_or_else(|| "online".to_string());

        let telemetry_metrics = payload.telemetry_metrics
            .or(payload.metrics)
            .unwrap_or_else(|| serde_json::json!({}));

        let current_parameters = payload.current_parameters
            .or(payload.parameters)
            .unwrap_or_else(|| serde_json::json!({}));

        let mut firmware_version = payload.firmware_version;
        if firmware_version.is_none() {
            if let Some(fw) = current_parameters.get("Device.DeviceInfo.SoftwareVersion").and_then(|v| v.as_str()) {
                firmware_version = Some(fw.to_string());
            }
        }

        let mut ip_address = payload.ip_address;
        if ip_address.is_none() {
            if let Some(ip) = current_parameters.get("Device.IP.Interface.1.IPv4Address.1.IPAddress").and_then(|v| v.as_str()) {
                ip_address = Some(ip.to_string());
            }
        }

        Ok(TelemetryUpdate {
            cpe_id: cpe_id.clone(),
            endpoint_id: Some(cpe_id),
            status,
            current_parameters,
            telemetry_metrics,
            ip_address,
            firmware_version,
            timestamp: chrono::Utc::now(),
        })
    }

    /// Decode BBF TR-369 Protobuf payload: Record -> NoSessionContextRecord -> Msg -> Notify
    fn decode_protobuf(fallback_cpe_id: &str, raw: &[u8]) -> Result<TelemetryUpdate, prost::DecodeError> {
        let record = Record::decode(raw)?;

        let cpe_id = if !record.from_id.is_empty() {
            record.from_id
                .trim_start_matches("proto::")
                .trim_start_matches("urn:bbf:usp:id:")
                .to_string()
        } else {
            fallback_cpe_id.to_string()
        };

        let endpoint_id = if !record.from_id.is_empty() {
            Some(record.from_id.clone())
        } else {
            Some(fallback_cpe_id.to_string())
        };

        let msg_bytes = match record.record_type {
            Some(record::RecordType::NoSessionContext(no_session)) => no_session.payload,
            Some(record::RecordType::SessionContext(session)) => {
                session.payload.into_iter().flatten().collect::<Vec<u8>>()
            }
            None => Vec::new(),
        };

        let mut current_parameters = serde_json::Map::new();
        let mut telemetry_metrics = serde_json::Map::new();
        let mut status = "online".to_string();

        if !msg_bytes.is_empty() {
            if let Ok(msg) = Msg::decode(&msg_bytes[..]) {
                if let Some(body) = msg.body {
                    match body.msg_body {
                        Some(body::MsgBody::Request(req)) => {
                            if let Some(request::ReqType::Notify(notify)) = req.req_type {
                                match notify.notification {
                                    Some(notify::Notification::Event(event)) => {
                                        for (k, v) in event.params {
                                            Self::process_param(&k, &v, &mut current_parameters, &mut telemetry_metrics);
                                        }
                                    }
                                    Some(notify::Notification::ValueChange(vc)) => {
                                        Self::process_param(&vc.param_path, &vc.param_value, &mut current_parameters, &mut telemetry_metrics);
                                    }
                                    _ => {}
                                }
                            }
                        }
                        Some(body::MsgBody::Response(resp)) => {
                            if let Some(response::RespType::GetResp(get_resp)) = resp.resp_type {
                                for path_result in get_resp.req_path_results {
                                    for res in path_result.resolved_path_results {
                                        for (k, v) in res.result_params {
                                            Self::process_param(&k, &v, &mut current_parameters, &mut telemetry_metrics);
                                        }
                                    }
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
        }

        let ip_address = current_parameters.get("Device.IP.Interface.1.IPv4Address.1.IPAddress")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let firmware_version = current_parameters.get("Device.DeviceInfo.SoftwareVersion")
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        Ok(TelemetryUpdate {
            cpe_id,
            endpoint_id,
            status,
            current_parameters: serde_json::Value::Object(current_parameters),
            telemetry_metrics: serde_json::Value::Object(telemetry_metrics),
            ip_address,
            firmware_version,
            timestamp: chrono::Utc::now(),
        })
    }

    /// Normalize TR-181 parameters into raw current_parameters and typed telemetry_metrics
    fn process_param(
        key: &str,
        val: &str,
        params: &mut serde_json::Map<String, serde_json::Value>,
        metrics: &mut serde_json::Map<String, serde_json::Value>,
    ) {
        // Retain raw TR-181 key-value pair
        params.insert(key.to_string(), serde_json::Value::String(val.to_string()));

        // Optical power normalization (triggers reconcile_live_to_history in PostgreSQL)
        if key.contains("Optical") && (key.ends_with("OpticalSignalLevel") || key.ends_with("RxPower")) {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("rx_optical_power".to_string(), serde_json::json!(num));
            }
        } else if key.contains("CPU") || key.ends_with("CPUUsage") {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("cpu_usage".to_string(), serde_json::json!(num));
            }
        } else if key.contains("Memory") && (key.ends_with("Free") || key.ends_with("MemoryUsage")) {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("memory_usage".to_string(), serde_json::json!(num));
            }
        } else if key.contains("Temperature") && key.ends_with("Value") {
            if let Ok(num) = val.parse::<f64>() {
                metrics.insert("temperature".to_string(), serde_json::json!(num));
            }
        } else if key.ends_with("BytesReceived") {
            if let Ok(num) = val.parse::<i64>() {
                metrics.insert("rx_bytes".to_string(), serde_json::json!(num));
            }
        } else if key.ends_with("BytesSent") {
            if let Ok(num) = val.parse::<i64>() {
                metrics.insert("tx_bytes".to_string(), serde_json::json!(num));
            }
        }
    }
}
```

---

### 3.5 PostgreSQL Writer & Idempotent UPSERT (`src/db.rs`)

To guarantee data integrity and prevent foreign key violations if telemetry arrives for an unprovisioned CPE:

```rust
use sqlx::{PgPool, Postgres, Transaction};
use crate::model::TelemetryUpdate;

pub async fn upsert_cpe_live_state(pool: &PgPool, update: &TelemetryUpdate) -> Result<(), sqlx::Error> {
    // 1. Ensure cpe_inventory stub exists (prevent foreign key violation 23503)
    sqlx::query!(
        r#"
        INSERT INTO cpe_inventory (cpe_id, serial_number, manufacturer, model, status)
        VALUES ($1, $1, 'Auto-Discovered', 'Generic-USP', 'online')
        ON CONFLICT (cpe_id) DO NOTHING
        "#,
        update.cpe_id
    )
    .execute(pool)
    .await?;

    // 2. Perform volatile in-RAM UPSERT into cpe_live_state
    sqlx::query!(
        r#"
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
            $1, $2, $3, $4, $5, $6, $7, NOW(), NOW()
        )
        ON CONFLICT (cpe_id) DO UPDATE SET
            endpoint_id = COALESCE(EXCLUDED.endpoint_id, cpe_live_state.endpoint_id),
            current_parameters = cpe_live_state.current_parameters || EXCLUDED.current_parameters,
            telemetry_metrics = cpe_live_state.telemetry_metrics || EXCLUDED.telemetry_metrics,
            status = EXCLUDED.status,
            ip_address = COALESCE(EXCLUDED.ip_address, cpe_live_state.ip_address),
            firmware_version = COALESCE(EXCLUDED.firmware_version, cpe_live_state.firmware_version),
            last_seen = NOW(),
            updated_at = NOW()
        "#,
        update.cpe_id,
        update.endpoint_id,
        update.current_parameters,
        update.telemetry_metrics,
        update.status,
        update.ip_address,
        update.firmware_version
    )
    .execute(pool)
    .await?;

    Ok(())
}
```

---

### 3.6 Production Multi-Stage Container (`rust-core/Dockerfile`)

```dockerfile
# Stage 1: Build binary with Alpine musl and protoc
FROM rust:1.77-alpine AS builder

RUN apk add --no-cache musl-dev protobuf-dev protoc build-base

WORKDIR /usr/src/rust-core

COPY Cargo.toml Cargo.lock* ./
RUN mkdir src proto && \
    echo 'syntax = "proto3"; package usp; message Record { string version = 1; }' > proto/usp.proto && \
    echo 'fn main() { prost_build::compile_protos(&["proto/usp.proto"], &["proto/"]).unwrap(); }' > build.rs && \
    echo 'fn main() {}' > src/main.rs && \
    cargo build --release && \
    rm -rf src proto build.rs

COPY proto ./proto
COPY build.rs ./build.rs
COPY src ./src

RUN touch src/main.rs build.rs && cargo build --release

# Stage 2: Minimal hardened runtime image
FROM alpine:3.19

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app

COPY --from=builder /usr/src/rust-core/target/release/rust-core /app/rust-core

# Healthcheck marker file expected by docker-compose.yml
ENV HEALTHCHECK_FILE=/tmp/healthy

CMD ["/app/rust-core"]
```

---

## 4. Caveats

1. **Protobuf Session Context Segmentation (SAR)**:
   - This design fully supports both `NoSessionContextRecord` and single-segment `SessionContextRecord`. In TR-369, multi-packet SAR (Segmentation and Reassembly) can occur if a payload exceeds the MTP MTU. Because Mosquitto defaults to a 100 MB max payload size, typical single-packet USP payloads do not require reassembly. Multi-packet SAR reassembly across multiple MQTT frames can be added if required by carrier network profiles.
2. **Postgres TLS & Network Environment**:
   - `runtime-tokio-rustls` was chosen over native-tls/OpenSSL to eliminate C runtime header dependencies during Alpine builds. This ensures cross-compilation reliability and reproducible container builds.
3. **Foreign Key Idempotency**:
   - `cpe_live_state` contains a foreign key to `cpe_inventory`. In the simulation flow, `simulate_flow.sh` registers the CPE in Step 1 before publishing telemetry in Step 2. However, for standalone MQTT simulations or unsolicited CPE boots, the worker includes an automatic `INSERT INTO cpe_inventory ... ON CONFLICT DO NOTHING` safeguard.

---

## 5. Conclusion

The Protobuf and payload decoding architecture for Milestone 3 (`rust-core`) is fully specified, robust, and wire-compatible:
1. **`proto/usp.proto`**: Self-contained schema retaining 100% BBF TR-369 1.3 tag numbers for `Record`, `NoSessionContextRecord`, `Msg`, `Header`, `Body`, `Request`, `Response`, `Notify`, and `Operate`.
2. **`build.rs`**: Clean compilation via `prost-build` with cache invalidation on `.proto` change.
3. **Dual Payload Decoding**: Robust byte-level heuristic enabling transparent ingestion of both BBF Protobuf binary payloads and `simulate_flow.sh` JSON payloads.
4. **Telemetry Extraction**: Dual normalization populating both raw TR-181 parameters and typed metrics (`rx_optical_power`), triggering the database optical reconciliation trigger upon variation > 1.0 dBm.
5. **Decoupled Tokio Pipeline**: MPSC channel (1024 depth) guarantees high-throughput MQTT ingestion without database back-pressure stalls.

All specifications are ready for direct implementation by `worker_m3`.

---

## 6. Verification Method

To independently verify the implementation:

1. **Protobuf Code Generation & Compilation Verification**:
   ```bash
   cd rust-core
   cargo check
   cargo build --release
   ```
   - Invalidation condition: `prost-build` compilation failure or missing `protoc` compiler.

2. **Unit Testing Dual Payload Decoding**:
   - Create tests in `rust-core/src/decoder.rs`:
     - Test A: Verify JSON deserialization of `simulate_flow.sh` Step 2 payload (`rx_optical_power: -18.5`, `cpu_usage: 42.5`).
     - Test B: Verify Protobuf deserialization of standard `Record` containing `NoSessionContextRecord` -> `Msg` -> `Notify` with `Device.Optical.Interface.1.OpticalSignalLevel = "-21.0"`.
     - Invalidation condition: Either test fails to extract `cpe_id` or optical telemetry.

3. **End-to-End Simulation Pipeline (`simulate_flow.sh`)**:
   ```bash
   bash simulate_flow.sh
   ```
   - Step 2: Publishes JSON telemetry.
   - Step 3: Verifies worker upserts `cpe_live_state` in RAM.
   - Step 4: Publishes altered optical telemetry and verifies reconciliation trigger creates entries in `cpe_state_history`.
   - Invalidation condition: Non-zero exit code or timeout.
