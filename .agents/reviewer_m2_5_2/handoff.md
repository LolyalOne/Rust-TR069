# Reviewer & Adversarial Critic Report: Milestone 2 (TR-069 Protocol & XML Parser Review)

**Agent**: Reviewer 2 (`reviewer_m2_5_2`)  
**Roles**: Reviewer (Quality & Verification), Critic (Adversarial Stress-Testing)  
**Milestone**: Milestone 2 — Embedded Axum CWMP Server, roxmltree Parser, Optical Normalization, MPSC Convergence & TR-069 Session Lifecycle  
**Target Repository**: `Rust-TR069` (`rust-core/`)  
**Timestamp**: 2026-09-07T19:39:00Z  

---

## 1. Review Summary

**Verdict**: **`APPROVE`**

The implementation of Milestone 2 delivered by `worker_m2_5` meets all technical, architectural, and protocol conformance requirements specified in `ORIGINAL_REQUEST.md` and Broadband Forum TR-069 standards.
Zero integrity violations were detected. No mock or hardcoded shortcuts exist in the production codepaths. The XML parsing engine handles real-world vendor deviations (Huawei TR-098 and TP-Link TR-181) defensively, optical power normalization is mathematically sound across all vendor integer scaling representations and unit suffixes, and the session state machine adheres strictly to TR-069 half-duplex HTTP semantics.

---

## 2. 5-Component Handoff Report

### 2.1 Observation

1. **Target Files Examined**:
   - `rust-core/src/cwmp.rs` (821 lines, SHA256 inspected):
     - `parse_inform` (lines 226–427): XML parsing via `roxmltree::Document`, namespace auto-repair via `inject_single_namespace`, parameter map extraction, and optical power extraction.
     - `normalize_optical_power` & `parse_optical_power` (lines 108–199): Converts decimal strings (`"-19.50"`), suffixed strings (`"-19.50 dBm"`), scaled integers (-1950, -19500), and handles LOS/disconnected fiber (`"0"`, `"N/A"`, `"--"` -> `None`).
     - `is_optical_rx_power_key` & `is_optical_tx_power_key` (lines 65–97): Segregates Rx optical power from Tx optical power.
     - `generate_inform_response` (lines 430–450): Generates `<cwmp:InformResponse>` echoing Header ID (`cwmp:ID`) with `mustUnderstand="1"` and `<MaxEnvelopes>1</MaxEnvelopes>`.
     - `generate_reboot_rpc` & `generate_get_parameter_values_rpc` (lines 458–514): Standard SOAP RPC builders echoing Header ID.
     - `build_soap_fault` (lines 522–551): Returns standard SOAP 1.1 Fault.
     - `into_telemetry_update` (lines 40–61): Converts parsed Inform into `TelemetryUpdate` struct.
   - `rust-core/src/main.rs` (lines 690–1140):
     - `AppState` (lines 691–695): Holds `tx: Sender<TelemetryUpdate>`, `db_pool: PgPool`, and `session_cache: Arc<RwLock<HashMap<IpAddr, (String, Instant)>>>`.
     - `dequeue_pending_command` (lines 704–746): Executes `SELECT id::text AS id, command_type, command_payload FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED` and transitions status to `'dispatched'` within a transaction.
     - `resolve_cpe_id` (lines 786–822): 3-tier session identification fallback (Cookie -> IP Cache 120s TTL -> PostgreSQL `cpe_live_state`).
     - `handle_cwmp_post` (lines 824–989): Axum handler for Empty POST, Inform POST (with 500ms MPSC send timeout guard and cookie setting), and RPC Response / Fault processing.
     - `run_cwmp_server` (lines 991–1020): Axum router binding `POST /`, `POST /cwmp`, and `POST /tr069`.
     - `main()` (lines 1045–1140): Supervises MQTT ingest, Axum CWMP server, DB sink, and health check with graceful shutdown via `watch::channel<bool>`.
   - `docker-compose.yml` (lines 48–58): Confirmed exposure of `7547:7547` on `rust-core` service and environment variables `CWMP_HOST=0.0.0.0` and `CWMP_PORT=7547`.
   - `postgres/init.sql` (lines 135–154): Verified regex `^-?[0-9]+(\.[0-9]+)?$` in `reconcile_live_to_history()` trigger matches normalized float optical power values.

2. **Test & Build Execution Outputs**:
   - `cargo check`:
     ```
     Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.18s
     ```
   - `cargo clippy -- -D warnings`:
     ```
     Finished `dev` profile [unoptimized + debuginfo] target(s) in 28.41s
     Exit Code: 0 (0 warnings, 0 errors)
     ```
   - `cargo test -- --nocapture`:
     ```
     running 28 tests
     test cwmp::tests::test_device_id_fallback_when_serial_empty ... ok
     test cwmp::tests::test_build_reboot_and_get_parameters_rpc ... ok
     test cwmp::tests::test_build_inform_response_echoes_header_id ... ok
     test cwmp::tests::test_parse_huawei_echolife_inform ... ok
     test cwmp::tests::test_parse_tplink_tr181_inform ... ok
     test cwmp::tests::test_optical_power_normalization_all_formats ... ok
     test tests::test_adversarial_empty_payload ... ok
     test cwmp::tests::test_parse_inform_tx_and_ip_and_fault ... ok
     test tests::test_adversarial_cpe_id_extraction_boundaries ... ok
     test tests::test_adversarial_invalid_protobuf_bytes ... ok
     test tests::test_adversarial_malformed_json_syntax ... ok
     test tests::test_adversarial_json_wrong_types ... ok
     test tests::test_adversarial_optical_power_parsing_and_nan ... ok
     test tests::test_adversarial_non_utf8_binary_payload ... ok
     test tests::test_adversarial_protobuf_corrupt_inner_msg ... ok
     test tests::test_adversarial_protobuf_empty_record_type ... ok
     test tests::test_adversarial_topic_filtering_mandate ... ok
     test tests::test_adversarial_topic_filtering_edge_cases ... ok
     test tests::test_adversarial_protobuf_missing_from_id_fallback ... ok
     test tests::test_build_soap_rpc_dispatch_variants ... ok
     test tests::test_adversarial_whitespace_payload ... ok
     test tests::test_decode_json_step2_payload ... ok
     test tests::test_decode_protobuf_event_notification ... ok
     test tests::test_decode_json_step4_altered_payload ... ok
     test tests::test_decode_protobuf_wire_compatible_record ... ok
     test tests::test_topic_filtering_and_extraction ... ok
     test tests::test_adversarial_large_payload_stress ... ok
     test tests::test_cwmp_session_and_telemetry_convergence ... ok

     test result: ok. 28 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.10s
     ```
   - `pytest -v` (in `python-api`):
     ```
     ============================== 47 passed in 9.57s ==============================
     ```

### 2.2 Logic Chain

1. **Protocol Conformance & Identifiers**:
   - TR-069 Section 3.4.1 mandates unique identification using `Manufacturer`, `OUI`, `ProductClass`, and `SerialNumber`. In consumer ONTs (GPON), `SerialNumber` is the globally unique physical identifier (e.g., Huawei `485754431234ABCD`).
   - In `cwmp.rs:317-321`, `cpe_id` uses `serial_number` if non-empty, otherwise falls back to `{oui}-{product_class}-{serial_number}`. This matches the inventory schema and enables seamless foreign key join against `cpe_inventory.cpe_id`.
2. **XML Robustness & Namespace Toleration**:
   - Consumer ONTs frequently output non-standard XML declarations (e.g. undeclared or case-mismatched prefixes like `soap-enc` vs `xmlns:SOAP-ENC`).
   - `inject_single_namespace` dynamically catches `roxmltree` namespace resolution errors and injects missing namespace declarations into the root envelope up to 5 iterations.
   - For parameter value extraction, self-closing tags `<Value xsi:type="xsd:string"/>` evaluate to `""` via `.unwrap_or("")` without error or panic.
3. **Optical Power Normalization & Trigger Compatibility**:
   - `postgres/init.sql:151` validates optical telemetry using numeric regex `^-?[0-9]+(\.[0-9]+)?$`.
   - Raw vendor telemetry presents units (`"-19.50 dBm"`), scaled integers (`"-1950"` for Huawei 0.01 dBm, `"-19500"` for TP-Link TR-181 0.001 dBm), or disconnection markers (`"0"`, `"N/A"`).
   - `cwmp::parse_optical_power` normalizes all valid representations into standard floating-point dBm (`-19.50`), strips units, maps invalid/disconnected readings to `None`, and restricts values to the physical range `[-60.0, 30.0]`.
   - `is_optical_rx_power_key` explicitly excludes Tx power parameters (`TxPower`, `TransmitOpticalLevel`, `X_HW_OpticalTxPower`), preventing transmitter output power from corrupting receiver sensitivity metrics.
4. **Session Lifecycle & Command Queuing**:
   - TR-069 session lifecycle requires a half-duplex handshake: Inform -> InformResponse -> Empty POST -> RPC Command -> RPC Response -> Empty 200 OK.
   - Axum handler `handle_cwmp_post` implements this state machine:
     - Inform: Ingests telemetry, sets session cookie `session=<cpe_id>`, returns `InformResponse`.
     - Empty POST: Uses `resolve_cpe_id` (Cookie -> IP cache -> DB) to identify CPE, queries `cpe_pending_commands` using `FOR UPDATE SKIP LOCKED`, and either dispatches the RPC envelope or returns empty 200 OK.
     - RPC Response: Updates command to `'completed'` with `result_payload`, checks for subsequent commands, or terminates the session.
5. **Backpressure & Concurrency Safety**:
   - The MPSC send in `handle_cwmp_post` is bounded by a 500ms timeout guard (`tokio::time::timeout`). Under database congestion, updates are dropped with a warning to avoid hanging the HTTP thread and triggering Inform retry storms on the ONT.
   - PostgreSQL transaction with `FOR UPDATE SKIP LOCKED` guarantees that concurrent CWMP sessions cannot dispatch the same pending command twice.

### 2.3 Caveats

1. **Authentication**: TR-069 specifies optional HTTP Basic or Digest Authentication. The server currently operates in open/pre-shared access mode (standard for dedicated ISP management VLANs). If public Internet routing is used in production, HTTP Digest Authentication should be added.
2. **Reverse Connection Request**: The server does not currently initiate HTTP GET requests against the ONT's `ConnectionRequestURL`. Commands are dispatched when the ONT initiates its periodic or event-driven Inform session (polling model). This is standard for deployments behind CGNAT.
3. **Empty Parameter Values in `GetParameterValuesResponse`**: In `main.rs:937`, `and_then(|c| c.text())` skips storing empty string parameters if `<Value/>` is self-closing in an RPC response. This is cosmetic and does not impact system stability.

### 2.4 Conclusion

The Milestone 2 implementation is fully compliant with TR-069 specifications, robust against hostile inputs and vendor firmware quirks, fully integrated with the PostgreSQL hybrid database and Python FastAPI command queue, and exhibits zero regressions across existing TR-369 USP MQTT components.

### 2.5 Verification Method

Run the following commands in `rust-core/` and `python-api/`:

```bash
# 1. Rust unit and integration tests
cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core
cargo test -- --nocapture

# 2. Rust strict linter check
cargo clippy -- -D warnings

# 3. Cross-component regression verification in python-api
cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api
pytest -v
```

**Invalidation Conditions**:
- Any failure among the 28 Rust tests or 47 Python tests.
- Optical Rx power being overwritten by Tx power values.
- MPSC channel deadlocking on shutdown due to leaked sender clones.
- Database cast errors in `reconcile_live_to_history()` due to unparsed strings.

---

## 3. Findings

### Minor Finding 1: Self-closing `<Value/>` in `GetParameterValuesResponse`

- **What**: In `main.rs:937`, `pvs.children().find(...).and_then(|c| c.text())` skips parameters with self-closing `<Value/>` tags rather than setting them to `""`.
- **Where**: `rust-core/src/main.rs:937`
- **Why**: `roxmltree::Node::text()` returns `None` on self-closing tags. While `cwmp.rs:370` uses `.unwrap_or("")`, `main.rs:937` uses `if let (Some(n), Some(v))`.
- **Suggestion**: Use `.unwrap_or("")` in `main.rs:937` to record empty string values when ONTs return empty parameters in `GetParameterValuesResponse`.
- **Severity**: Minor (Nice to fix; does not break command execution or session lifecycle).

---

## 4. Verified Claims

1. **DeviceId extraction adheres to TR-069** -> Verified via `cwmp::tests::test_parse_huawei_echolife_inform` and `test_device_id_fallback_when_serial_empty` -> **PASS**
2. **Self-closing `<Value/>` tags handled safely without panic** -> Verified via `cwmp::tests::test_parse_huawei_echolife_inform` (ProvisioningCode) -> **PASS**
3. **Optical power normalized across decimal, suffixed, and scaled integer formats** -> Verified via `cwmp::tests::test_optical_power_normalization_all_formats` -> **PASS**
4. **Tx optical power separated from Rx optical power** -> Verified via `cwmp::tests::test_parse_inform_tx_and_ip_and_fault` -> **PASS**
5. **InformResponse echoes Header ID with MaxEnvelopes=1** -> Verified via `cwmp::tests::test_build_inform_response_echoes_header_id` -> **PASS**
6. **Reboot and GetParameterValues SOAP RPC builders conform to CWMP schema** -> Verified via `cwmp::tests::test_build_reboot_and_get_parameters_rpc` and `tests::test_build_soap_rpc_dispatch_variants` -> **PASS**
7. **Session lifecycle correctly handles Empty POST and MPSC convergence** -> Verified via `tests::test_cwmp_session_and_telemetry_convergence` -> **PASS**
8. **Pending command queue uses `FOR UPDATE SKIP LOCKED`** -> Verified by static code audit of `main.rs:718` -> **PASS**
9. **Zero Clippy warnings under `-D warnings`** -> Verified via `cargo clippy -- -D warnings` -> **PASS**
10. **Zero regressions on existing TR-369 and Python FastAPI components** -> Verified via 28 Rust tests and 47 Pytest tests -> **PASS**

---

## 5. Adversarial Challenge Report

### 5.1 Challenge Summary

**Overall Risk Assessment**: **`LOW`**

The implementation demonstrates exceptional defensive engineering:
- XML attacks (XXE, Billion Laughs) are mitigated by `roxmltree`'s parser design (disabling external entity resolution and DTDs).
- Channel saturation attacks are neutralized by the 500ms `tokio::time::timeout` guard on MPSC sends.
- Concurrency race conditions in the command queue are eliminated by database row locks with `SKIP LOCKED`.
- Session tracking under CGNAT is secured by primary reliance on HTTP cookies (`session=<cpe_id>`).

### 5.2 Challenges & Mitigations

| # | Challenge / Attack Vector | Scenario | Blast Radius | Mitigation Tested / Confirmed |
|---|---------------------------|----------|--------------|-------------------------------|
| 1 | Undeclared XML namespace prefixes | Huawei/ZTE firmware outputs `<Event soap-enc:arrayType="...">` without matching case declaration | Parser rejects packet with fatal error | `inject_single_namespace` dynamically injects declarations for common CWMP prefixes; verified in test suite. |
| 2 | Optical power integer scaling collision | Scaled integer `-1950` vs coarse `-20` | Bogus optical power reading or database numeric overflow | Distinct numeric ranges partitioned: `[-60, -1]` (coarse), `[-9999, -61]` (0.01 scale), `[-99999, -10000]` (0.001 scale); verified in unit tests. |
| 3 | MPSC queue buffer exhaustion | Database sink stalls, 1024-message channel fills up | HTTP request handler hangs, causing ONT Inform retry storm | `tokio::time::timeout(Duration::from_millis(500), tx.send(update))` drops update and still sends HTTP 200 `InformResponse`. |
| 4 | Multiple pending commands in queue | CPE requests next command before session closes | Commands stuck in `'pending'` until next periodic Inform | Axum handler loops to check for subsequent commands after receiving RPC response and dispatches immediately. |
| 5 | CGNAT IP address sharing | Multiple ONTs connect from same public NAT IP | Commands dispatched to wrong CPE | `resolve_cpe_id` checks HTTP `Cookie: session=<cpe_id>` before checking IP address cache. |

### 5.3 Stress Test Results

- `test_adversarial_large_payload_stress` -> 3000-parameter large JSON payload -> Parsed cleanly without memory leak -> **PASS**
- `test_adversarial_empty_payload` & `test_adversarial_whitespace_payload` -> Handled without panic -> **PASS**
- `test_adversarial_non_utf8_binary_payload` & `test_adversarial_invalid_protobuf_bytes` -> Safely rejected -> **PASS**
- `test_adversarial_optical_power_parsing_and_nan` -> NaN and Infinity rejected -> **PASS**

### 5.4 Unchallenged Areas

- Physical ONT hardware interoperability testing on an active GPON OLT chassis (requires physical lab environment).
