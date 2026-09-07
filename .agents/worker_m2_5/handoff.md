# Milestone 2 Implementation Handoff Report: Dual-Stack Rust Core CWMP Server & XML Telemetry Ingest

**Agent**: Worker M2 (`worker_m2_5`)  
**Milestone**: Milestone 2 — Embedded Axum CWMP Server, roxmltree Parser, Optical Normalization, MPSC Convergence & Pending Command Delivery  
**Target Repository**: `Rust-TR069` (`rust-core/`)  
**Timestamp**: 2026-09-07T19:35:00Z  

---

## 1. Observation

### 1.1 Modified & Created Source Files
1. **`rust-core/Cargo.toml` (lines 25–27)**:
   Added Axum and roxmltree dependencies:
   ```toml
   anyhow = "1.0"
   axum = "0.7"
   roxmltree = "0.20"
   ```
2. **`rust-core/src/cwmp.rs` (created, 822 lines)**:
   Implemented:
   - `parse_inform(xml_str: &str) -> Result<ParsedInform>` extracting `cpe_id`, `manufacturer`, `oui`, `product_class`, `serial_number`, `header_id`, `events`, `parameters`, `rx_optical_power`, `tx_optical_power`, `ip_address`, `software_version`, and `hardware_version`.
   - `normalize_optical_power(key: &str, val: &str) -> Option<f64>` and `parse_optical_power(raw: &str) -> Option<f64>` converting decimal strings (`"-19.50"`), suffixed strings (`"-19.50 dBm"`), Huawei 0.01 dBm scaled integers (`"-1950"`), and TP-Link 0.001 dBm scaled integers (`"-19500"`) into standard `f64` values.
   - `is_optical_rx_power_key(key: &str) -> bool` and `is_optical_tx_power_key(key: &str) -> bool` to prevent Tx optical power from colliding with or overwriting Rx optical power.
   - `generate_inform_response(id: &str) -> String` and `build_inform_response(id: &str) -> String` producing standard `<cwmp:InformResponse>` echoing the header ID.
   - `generate_reboot_rpc(id: &str, command_key: &str) -> String` producing standard `<cwmp:Reboot>`.
   - `generate_get_parameter_values_rpc(id: &str, names: &[String]) -> String` producing standard `<cwmp:GetParameterValues>`.
   - `build_soap_fault(fault_code: &str, fault_string: &str, header_id: Option<&str>) -> String`.
   - Resilient XML parsing via `inject_single_namespace` to dynamically resolve missing or case-mismatched namespace prefixes (e.g. `xmlns:SOAP-ENC` vs `soap-enc:arrayType`).
   - `into_telemetry_update(self, client_ip: Option<String>) -> TelemetryUpdate` method bridging parsed CWMP data directly into the MPSC pipeline.
3. **`rust-core/src/main.rs` (lines 20, 684–880)**:
   Implemented:
   - Declared `pub mod cwmp;`.
   - Defined `AppState` with `tx: Sender<TelemetryUpdate>`, `db_pool: PgPool`, and `session_cache: Arc<RwLock<HashMap<IpAddr, (String, Instant)>>>`.
   - `dequeue_pending_command(&PgPool, &str)` using PostgreSQL transaction:
     ```sql
     SELECT id::text AS id, command_type, command_payload
     FROM cpe_pending_commands
     WHERE cpe_id = $1 AND status = 'pending'
     ORDER BY created_at ASC
     LIMIT 1
     FOR UPDATE SKIP LOCKED
     ```
     Transitioning matched commands to `status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP`.
   - `build_soap_rpc_for_command(&PendingCommandRow) -> String`.
   - `resolve_cpe_id(&HeaderMap, IpAddr, &AppState) -> Option<String>` with 3-tier fallback:
     1. HTTP Cookie header (`session=<cpe_id>`)
     2. In-memory session cache (IP -> CPE ID, 120s TTL)
     3. PostgreSQL fallback lookup on `cpe_live_state WHERE ip_address = $1`
   - Axum handler `handle_cwmp_post` handling:
     1. Empty POST (`body.is_empty()`): queries pending commands and returns SOAP RPC or empty 200 OK (`Content-Length: 0`).
     2. Inform POST: parses XML via `cwmp::parse_inform`, constructs `TelemetryUpdate`, dispatches to MPSC with 500ms timeout guard, caches session, and returns HTTP 200 OK with `InformResponse` and `Set-Cookie: session=<cpe_id>; Path=/; HttpOnly`.
     3. RPC Response POST (`RebootResponse`, `GetParameterValuesResponse`, `Fault`): updates command status in `cpe_pending_commands` to `completed` or `failed` with `completed_at` and `result_payload`, and dispatches next pending command or returns empty 200 OK.
   - `run_cwmp_server` on port 7547 (configurable via `CWMP_HOST` and `CWMP_PORT`), routing `POST /`, `POST /cwmp`, and `POST /tr069`.
   - Dual-stack task supervisor in `main()` with graceful shutdown: `axum_tx = tx.clone()` passed to Axum server; original `tx` passed to MQTT task; `main()` retains no sender handles; on `ctrl_c`, both MQTT and Axum servers shutdown and drop senders, allowing `run_db_sink` to drain completely.

### 1.2 Verification Tool Output
Executing `cargo check`:
```
    Checking rust-core v0.1.0 (/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 8.63s
```
Executing `cargo clippy -- -D warnings`:
```
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 13.22s
```
Executing `cargo test -- --nocapture`:
```
running 28 tests
test cwmp::tests::test_build_inform_response_echoes_header_id ... ok
test cwmp::tests::test_build_reboot_and_get_parameters_rpc ... ok
test cwmp::tests::test_device_id_fallback_when_serial_empty ... ok
test cwmp::tests::test_optical_power_normalization_all_formats ... ok
test cwmp::tests::test_parse_tplink_tr181_inform ... ok
test cwmp::tests::test_parse_huawei_echolife_inform ... ok
test tests::test_adversarial_empty_payload ... ok
test tests::test_adversarial_cpe_id_extraction_boundaries ... ok
test cwmp::tests::test_parse_inform_tx_and_ip_and_fault ... ok
test tests::test_adversarial_invalid_protobuf_bytes ... ok
test tests::test_adversarial_json_wrong_types ... ok
test tests::test_adversarial_malformed_json_syntax ... ok
test tests::test_adversarial_non_utf8_binary_payload ... ok
test tests::test_adversarial_optical_power_parsing_and_nan ... ok
test tests::test_adversarial_protobuf_corrupt_inner_msg ... ok
test tests::test_adversarial_protobuf_empty_record_type ... ok
test tests::test_adversarial_protobuf_missing_from_id_fallback ... ok
test tests::test_adversarial_topic_filtering_edge_cases ... ok
test tests::test_adversarial_topic_filtering_mandate ... ok
test tests::test_adversarial_whitespace_payload ... ok
test tests::test_build_soap_rpc_dispatch_variants ... ok
test tests::test_decode_json_step2_payload ... ok
test tests::test_decode_json_step4_altered_payload ... ok
test tests::test_decode_protobuf_event_notification ... ok
test tests::test_decode_protobuf_wire_compatible_record ... ok
test tests::test_topic_filtering_and_extraction ... ok
test tests::test_adversarial_large_payload_stress ... ok
test tests::test_cwmp_session_and_telemetry_convergence ... ok

test result: ok. 28 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s
```

---

## 2. Logic Chain

1. **Dual-Stack Ingress Convergence**:
   - The user requested supporting classical TR-069 consumer ONTs (Huawei EchoLife, TP-Link) while maintaining zero regression on the existing TR-369 USP MQTT pipeline.
   - In `main.rs`, the MPSC channel `(tx, rx)` was created with capacity 1024. Cloning `tx` to `axum_tx` and injecting it into `AppState` allows the Axum HTTP handler to send homogeneous `TelemetryUpdate` structs into the same channel consumed by `run_db_sink`.
   - As observed in test `test_cwmp_session_and_telemetry_convergence`, parsed CWMP Informs are translated into `TelemetryUpdate` and successfully enqueued and dequeued through the channel without database schema modifications.

2. **Optical Power Normalization Resilience**:
   - The database trigger `reconcile_live_to_history()` inspects `telemetry_metrics->>'rx_optical_power'` and casts to numeric. Unscaled integers (e.g. `-1950` or `-19500`) or strings with units (`"-19.50 dBm"`) would cause catastrophic database cast errors or spurious reconciliation thrashing.
   - `cwmp::parse_optical_power` strips units (`dBm`, `dB`), handles integer scaling (detecting `< -100` for 0.01 scale and `< -10000` for 0.001 millidBm scale), and maps disconnected fiber readings (`"0"`, `"N/A"`, `"--"`) to `None`.
   - Differentiating `is_optical_rx_power_key` and `is_optical_tx_power_key` guarantees that Tx optical power readings never overwrite the Rx optical power metric.

3. **Pending Commands Concurrency & FIFO Ordering**:
   - As designed in Milestone 1 and explorer reports, `cpe_pending_commands` stores queued RPCs.
   - In `dequeue_pending_command`, the query `SELECT id::text AS id, command_type, command_payload FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED` guarantees non-blocking, race-condition-free FIFO command dispatch.
   - The command status is atomically transitioned to `'dispatched'` before returning the SOAP envelope.
   - Upon receiving `cwmp:RebootResponse` or `cwmp:GetParameterValuesResponse`, the command status transitions to `'completed'` with `completed_at` and `result_payload`.

4. **Graceful Shutdown & Leak Prevention**:
   - Tokio MPSC channels only return `None` on `recv()` when ALL senders are dropped.
   - By avoiding holding any `tx` sender in `main()`, only `run_mqtt_ingest` and the Axum `AppState` hold senders.
   - When `shutdown_tx.send(true)` fires on `ctrl_c`, Axum completes inflight requests and terminates (dropping its sender), MQTT terminates (dropping its sender), and `run_db_sink` drains all queued updates to Postgres before cleanly exiting.

---

## 3. Caveats

- **Network Port 7547 in Local Test Environments**:
  In bare-metal local development without Docker, port 7547 requires binding privileges or an alternate `CWMP_PORT` environment variable (e.g. `CWMP_PORT=7548`). In Docker, `7547:7547` is already configured in `docker-compose.yml`.
- **Session Identification without Cookie**:
  If a non-compliant ONT drops the `Set-Cookie` header during the empty POST phase, the system falls back to the in-memory IP session cache (120s TTL) and subsequently the PostgreSQL `cpe_live_state` IP lookup.

---

## 4. Conclusion

Milestone 2 implementation is completely finished and verified:
1. `rust-core/Cargo.toml` is updated with `axum = "0.7"` and `roxmltree = "0.20"`.
2. `rust-core/src/cwmp.rs` provides complete XML Inform parsing, vendor namespace auto-repair, optical telemetry normalization, and SOAP envelope builders (`InformResponse`, `Reboot`, `GetParameterValues`, `Fault`).
3. `rust-core/src/main.rs` embeds the Axum HTTP server on port 7547, manages the full TR-069 session lifecycle, converges telemetry updates into the MPSC sink, and dispatches/completes commands via `cpe_pending_commands` with `FOR UPDATE SKIP LOCKED`.
4. 100% test pass rate across 28 unit and integration tests (zero regressions on existing TR-369 tests, 9 new CWMP tests passed).

---

## 5. Verification Method

To independently verify the implementation:

```bash
cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core

# 1. Check compilation and strict linting
cargo check
cargo clippy -- -D warnings

# 2. Run all unit and integration tests
cargo test -- --nocapture
```

### Invalidation Conditions
- Any test failure in the 28 unit tests.
- Any regression in MQTT TR-369 Protobuf parsing or JSON decoding.
- Spurious optical power values exceeding [-60, +30] dBm or unparsed unit suffixes.
- Senders failing to close on shutdown causing `run_db_sink` to hang.
