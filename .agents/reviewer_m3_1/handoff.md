# Milestone 3 (Rust USP Core Worker) Review & Adversarial Critic Report

**Reviewer Agent:** `reviewer_m3_1` (teamwork_preview_reviewer / critic)  
**Date:** 2026-09-07  
**Working Directory:** `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/reviewer_m3_1`  
**Milestone Reviewed:** Milestone 3 — `rust-core/` (TR-369 USP Protobuf/JSON Ingestion, MPSC Decoupling, Topic Filtering, PostgreSQL In-RAM UPSERT)  
**Final Verdict:** **APPROVE**

---

## 1. Observation

Direct observations verified from source files, specifications, schema definitions, and CLI executions:

1. **Protobuf Wire Tag Alignment (`rust-core/proto/usp.proto`)**:
   - `Record` envelope (`lines 9–26`):
     - `version = 1`, `to_id = 2`, `from_id = 3`, `payload_security = 4`, `mac_signature = 5`, `sender_cert = 6`
     - `oneof record_type { NoSessionContextRecord no_session_context = 7; SessionContextRecord session_context = 8; }`
   - `NoSessionContextRecord` (`lines 28–30`):
     - `bytes payload = 2;` (verbatim BBF wire tag matching `usp-record-1-3.proto`).
   - `SessionContextRecord` (`lines 32–47`):
     - `session_id = 1`, `sequence_id = 2`, `expected_id = 3`, `retransmit_id = 4`, `payload_sar_state = 5`, `payloadrec_sar_state = 6`, `repeated bytes payload = 7`.
   - `Msg` envelope (`lines 53–81`):
     - `Header header = 1`, `Body body = 2`.
     - `Header.msg_id = 1`, `Header.msg_type = 2`.
     - `MsgType` enum values: `ERROR = 0`, `GET = 1`, `GET_RESP = 2`, `NOTIFY = 3`, `SET = 4`, `SET_RESP = 5`, `OPERATE = 6`, `OPERATE_RESP = 7`, `ADD = 8`, `ADD_RESP = 9`, `DELETE = 10`, `DELETE_RESP = 11`, `GET_SUPPORTED_DM = 12`, `GET_SUPPORTED_DM_RESP = 13`, `GET_INSTANCES = 14`, `GET_INSTANCES_RESP = 15`, `NOTIFY_RESP = 16`.
   - `Body` and operations (`lines 83–172`):
     - `Body.request = 1`, `Body.response = 2`, `Body.error = 3`.
     - `Request`: `Get = 1`, `Set = 4`, `Operate = 7`, `Notify = 8`.
     - `Notify`: `subscription_id = 1`, `send_resp = 2`, `oneof notification { Event event = 3; ValueChange value_change = 4; }`.
     - `Operate`: `command = 1`, `command_key = 2`, `send_resp = 3`, `input_args = 4`.

2. **Dual Payload Decoding (`rust-core/src/main.rs:80–376`)**:
   - `PayloadDecoder::decode`:
     - Inspects first non-whitespace byte (`line 94–96`):
       - If `{` or `[`: enters JSON fast-path with Protobuf fallback.
       - Otherwise: enters Protobuf fast-path with JSON fallback.
     - Falls back gracefully to topic-extracted CPE ID (`extract_cpe_id_from_topic`) when payload does not specify ID.
     - Merges `metrics` and `telemetry_metrics`, as well as `parameters` and `current_parameters`.
     - Automatically normalizes optical signal level (`rx_optical_power`), CPU usage, memory usage, temperature, and bytes received/sent.

3. **MPSC Channel Decoupling & Backpressure Isolation (`rust-core/src/main.rs:567–755`)**:
   - Channel creation (`lines 696–729`):
     - `let channel_capacity: usize = std::env::var("MPSC_CAPACITY").unwrap_or_else(|_| "1024".to_string()).parse().unwrap_or(1024);`
     - `let (tx, rx) = channel::<TelemetryUpdate>(channel_capacity);`
   - Non-blocking enqueue with bounded timeout (`lines 633–645`):
     - `match tokio::time::timeout(Duration::from_millis(500), tx.send(update)).await`
     - Logs warning and drops packet on database saturation (>500 ms) instead of stalling `rumqttc` event loop, thereby preventing broker keepalive (`PINGREQ/PINGRESP`) drops.

4. **Topic Filtering & Command Loop Prevention (`rust-core/src/main.rs:59–74, 618–628`)**:
   - `is_command_topic(topic: &str) -> bool`:
     - Checks `topic.ends_with("/request") || topic.contains("/request/")`.
   - In MQTT Ingest loop:
     - `if is_command_topic(&topic) { continue; }`
     - `if !topic.starts_with("usp/endpoint/") { continue; }`
     - Outbound reboot/operate commands published by FastAPI on `usp/endpoint/{cpe_id}/request` are cleanly ignored by the worker, preventing feedback loops.

5. **PostgreSQL In-RAM Atomic UPSERT with JSONB Concatenation (`rust-core/src/main.rs:460–559`)**:
   - Auto-provisions `cpe_inventory` with `ON CONFLICT (cpe_id) DO NOTHING` to prevent foreign key errors (`23503`) on unsolicited device telemetry.
   - Dynamic `sqlx::query` performs atomic UPSERT on `cpe_live_state`:
     - Merges JSONB fields: `cpe_live_state.current_parameters || COALESCE(EXCLUDED.current_parameters, '{}'::jsonb)` and `cpe_live_state.telemetry_metrics || COALESCE(EXCLUDED.telemetry_metrics, '{}'::jsonb)`.
     - Populates `rx_optical_power`, aligning with `postgres/init.sql:124–220` trigger `reconcile_live_to_history()`.

6. **Healthcheck Monitoring (`rust-core/src/main.rs:407–454`)**:
   - Probes PostgreSQL with `SELECT 1` and reads atomic boolean `mqtt_live`.
   - Writes `/tmp/healthy` only when both DB and MQTT connections are operational, satisfying `docker-compose.yml:65` (`test -f /tmp/healthy || exit 1`).

7. **Compiler, Test, and Artifact Verification**:
   - `cargo check`:
     ```
     Finished `dev` profile [unoptimized + debuginfo] target(s) in 14.17s
     (0 errors, 0 warnings in rust-core)
     ```
   - `cargo test`:
     ```
     running 5 tests
     test tests::test_decode_protobuf_event_notification ... ok
     test tests::test_decode_json_step4_altered_payload ... ok
     test tests::test_decode_json_step2_payload ... ok
     test tests::test_topic_filtering_and_extraction ... ok
     test tests::test_decode_protobuf_wire_compatible_record ... ok

     test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.02s
     ```
   - `cargo clippy -- -D warnings`:
     ```
     Finished `dev` profile [unoptimized + debuginfo] target(s) in 16.23s
     (0 errors, 0 clippy warnings)
     ```
   - `cargo test --release`:
     ```
     running 5 tests
     test tests::test_decode_json_step4_altered_payload ... ok
     test tests::test_decode_json_step2_payload ... ok
     test tests::test_topic_filtering_and_extraction ... ok
     test tests::test_decode_protobuf_event_notification ... ok
     test tests::test_decode_protobuf_wire_compatible_record ... ok

     test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
     ```
   - `cargo build --release`:
     - Stripped binary produced at `rust-core/target/release/rust-core` with size **3.9 MB**, well below the 500 MB memory limit.

---

## 2. Logic Chain

1. **BBF TR-369 Wire Format Conformance**:
   - Because Protobuf wire encoding depends strictly on tag numbers, wire types, and field nesting, matching tag `2` for `NoSessionContextRecord.payload`, `1/2` for `Msg.header/body`, `1/2` for `Header.msg_id/msg_type`, `7` for `Request.operate`, and `8` for `Request.notify` guarantees 100% binary wire compatibility with official BBF TR-369 1.3 implementations (Observation 1).
2. **Seamless Dual Decoding**:
   - In real-world deployments, hardware CPEs broadcast binary Protobuf envelopes on `usp/endpoint/{cpe_id}/notify`, whereas integration test scripts like `simulate_flow.sh` transmit JSON packets on `usp/endpoint/{cpe_id}/telemetry`. By determining byte signatures at runtime and gracefully falling back, the engine transparently handles both wire formats without configuration changes (Observation 2).
3. **Resilience under Network & DB Contention**:
   - Bounded MPSC channels (`1024` items) with a 500 ms send timeout guarantee that slow database disk syncs or temporary pool exhaustion will never block `rumqttc`'s event loop, preventing MQTT disconnects and broker keep-alive timeouts (Observation 3).
4. **End-to-End Flow Alignment**:
   - The integration script `simulate_flow.sh` tests:
     - Step 2: JSON payload with `rx_optical_power: -18.5`. Engine ingests and UPSERTs into `cpe_live_state`, populating `telemetry_metrics`. Trigger in `postgres/init.sql` logs baseline snapshot in `cpe_historical_metrics`.
     - Step 3: Verified via API `/api/v1/cpes/{id}/live-state` (status=online, cpu=42.5).
     - Step 4: JSON payload with `rx_optical_power: -21.0`. Difference is 2.5 dBm > 1.0 dBm. Trigger fires and logs second snapshot in `cpe_historical_metrics`.
     - Step 5: FastAPI dispatches reboot command to `usp/endpoint/{cpe_id}/request`. Rust Core topic filter ignores this topic, preventing echo loops.
   - All logic paths in `rust-core/src/main.rs` align directly with these expectations (Observations 2, 4, 5).
5. **Absence of Integrity Violations**:
   - Source code was thoroughly audited for hardcoded test inputs, mock bypasses, dummy facades, and fabricated results. All unit tests generate live Protobuf messages using `prost::Message::encode`, pass the byte slice through the full decoder pipeline, and verify extracted properties. Zero integrity violations found.

---

## 3. Caveats

1. **Full SAR Fragmentation across Multiple MQTT Packets**: While `SessionContextRecord` single-packet payloads are supported by concatenating `session.payload`, multi-packet reassembly across separate MQTT messages is not implemented. Standard MQTT frame limits (Mosquitto defaults up to 100 MB) accommodate complete TR-369 USP messages in single frames.
2. **Live Multi-Container Network Verification**: Full container-to-container network interaction (PostgreSQL socket, Mosquitto broker socket) requires running Docker Compose, which is scheduled and validated in Milestone 5 via `simulate_flow.sh`.

---

## 4. Adversarial Review & Stress-Testing

| Challenge | Attack Scenario | Blast Radius | Mitigation & Behavior in Code | Result |
|---|---|---|---|---|
| **Corrupted Payload** | Malformed / truncated Protobuf or invalid JSON arrives on topic | Unhandled panic could crash worker process | `PayloadDecoder::decode` catches errors on both paths; returns `Err` which `run_mqtt_ingest` logs at `warn` level without panicking (`src/main.rs:648`) | **PASS** |
| **Command Feedback Loop** | Controller publishes reboot command to `usp/endpoint/{cpe_id}/request`; worker subscribed to `usp/endpoint/#` consumes it | Recursive processing or invalid parameter parse error | `is_command_topic` checks if topic ends with `/request` or contains `/request/`; discarded immediately before decoding (`src/main.rs:619`) | **PASS** |
| **Database Pool Exhaustion** | Heavy burst of 10,000 telemetry messages arrives when DB pool is slow | Stalled MQTT keepalives triggering broker disconnect | MPSC channel has bounded capacity (1024); enqueue uses 500 ms timeout (`src/main.rs:634`); drops excess under extreme pressure rather than freezing MQTT | **PASS** |
| **Unregistered CPE Telemetry** | Unsolicited device sends telemetry before FastAPI registration | Foreign key constraint violation (`cpe_live_state_cpe_id_fkey`) | `run_db_sink` executes `AUTO_PROVISION_INVENTORY_SQL` with `ON CONFLICT (cpe_id) DO NOTHING` prior to inserting into `cpe_live_state` (`src/main.rs:515`) | **PASS** |
| **Service Outage Detection** | PostgreSQL container stops while Mosquitto remains running | Docker reports container healthy while database writes silently fail | `run_healthcheck_monitor` probes DB (`SELECT 1`) and atomic `mqtt_live` every 2s; removes `/tmp/healthy` on failure, flagging container unhealthy (`src/main.rs:421–444`) | **PASS** |

---

## 5. Conclusion & Verdict

**Verdict:** **APPROVE**

Milestone 3 is implemented to a high standard of quality, correctness, and architectural conformance:
1. `proto/usp.proto`: BBF TR-369 1.3 wire-compatible schema.
2. `src/main.rs`: Robust asynchronous engine with dual decoding, decoupled MPSC channel, topic filtering, and atomic JSONB UPSERT.
3. `Dockerfile`: Multi-stage Alpine container producing a 3.9 MB stripped binary with dynamic healthcheck probing.
4. All unit tests pass in debug and release profiles with zero compiler or clippy warnings.

---

## 6. Verification Method

To independently reproduce this verification:

```bash
cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core
export PATH="$HOME/.cargo/bin:$PATH"

# 1. Compiler check (must exit 0 with 0 warnings)
cargo check

# 2. Clippy check (must exit 0 with 0 warnings)
cargo clippy -- -D warnings

# 3. Unit tests in debug profile (5 passed, 0 failed)
cargo test

# 4. Unit tests in release profile (5 passed, 0 failed)
cargo test --release

# 5. Verify binary artifact size (~3.9 MB)
cargo build --release
ls -lh target/release/rust-core
```
