# Scope: Milestone 2 — Rust Core CWMP Server & XML Ingest

## Architecture
Rust Core operates as a dual-stack ingress daemon:
1. **MQTT Subscriber (TR-369 USP)**: Reads Protobuf records from Mosquitto broker.
2. **Axum HTTP Server (TR-069 CWMP)**: Binds to `0.0.0.0:7547` (configurable via `CWMP_PORT`), receiving SOAP/XML requests (`cwmp:Inform`, empty POST).
3. **MPSC Convergence**: Both protocols construct homogeneous `TelemetryUpdate` structs and send them through the cloned `tokio::sync::mpsc::Sender<TelemetryUpdate>` to `run_db_sink`.
4. **Pending Command Query**: On receiving an Inform or empty POST, `rust-core` queries PostgreSQL table `cpe_pending_commands` for any pending command for the ONT's serial number, updates status, and returns the appropriate SOAP RPC payload or InformResponse.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F4 | Axum Embedded HTTP Server | Embedded Axum HTTP server running on 0.0.0.0:7547 in Tokio runtime alongside MQTT | M2 | ORIGINAL_REQUEST R1 |
| F5 | Fast XML Parsing Engine | `roxmltree` or `quick-xml` zero-allocation parser resilient to vendor namespace prefixes | M2 | ORIGINAL_REQUEST R2 |
| F6 | Huawei EchoLife Inform Parser | Extract DeviceId (`SerialNumber`, `Manufacturer`) and TR-098 Optical params (`X_HW_OpticalRxPower`) | M2 | ORIGINAL_REQUEST R2 |
| F7 | TP-Link Inform Parser | Extract DeviceId and TR-181 Optical params (`Device.Optical.Interface.1.OpticalSignalLevel`) | M2 | ORIGINAL_REQUEST R2 |
| F8 | MPSC Unified Ingestion | Converge parsed CWMP Inform data into `tokio::sync::mpsc::Sender<TelemetryUpdate>` | M2 | ORIGINAL_REQUEST R3 |
| F9 | CWMP InformResponse & Command Delivery | Return valid `<cwmp:InformResponse>` or pending command RPC (`GetParameterValues`, `Reboot`) from `cpe_pending_commands` | M2 | ORIGINAL_REQUEST R3 |

## Interface Contracts
### `rust-core` HTTP Server ↔ `run_db_sink` (MPSC Channel)
- Channel: `tokio::sync::mpsc::Sender<TelemetryUpdate>`
- `TelemetryUpdate` fields populated from XML Inform:
  - `cpe_id`: SerialNumber (e.g. `485754431234ABCD`)
  - `endpoint_id`: Option with `cpe_id` or OUI-ProductClass-SerialNumber
  - `status`: `"online"`
  - `current_parameters`: JSON object with all extracted `<ParameterValueStruct>` key-values
  - `telemetry_metrics`: JSON object with `"rx_optical_power"` parsed as float, e.g. `-19.50`
  - `ip_address`: extracted from connection or `ConnectionRequestURL`
  - `firmware_version`: extracted from SoftwareVersion
  - `timestamp`: Utc::now()

### `rust-core` ↔ PostgreSQL (`cpe_pending_commands`)
- Query pending command:
  ```sql
  SELECT id, command_type, command_payload
  FROM cpe_pending_commands
  WHERE cpe_id = $1 AND status = 'pending'
  ORDER BY created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED;
  ```
- Mark command dispatched:
  ```sql
  UPDATE cpe_pending_commands
  SET status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP
  WHERE id = $1;
  ```
