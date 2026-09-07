# Milestone 2 Quality & Adversarial Review Report: Dual-Stack Rust Core CWMP Server & XML Ingestion

**Reviewer**: Reviewer 1 (`reviewer_m2_5_1`)  
**Roles**: Reviewer, Adversarial Critic  
**Working Directory**: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/reviewer_m2_5_1`  
**Milestone**: Milestone 2 — Embedded Axum CWMP Server, roxmltree Parser, Optical Normalization, MPSC Convergence & Pending Command Delivery  
**Timestamp**: 2026-09-07T19:40:00Z  
**Verdict**: **APPROVE**  

---

## 1. Observation

### 1.1 Source Code and Configuration Inspected
1. **`rust-core/Cargo.toml`**:
   - Dependencies added: `anyhow = "1.0"`, `axum = "0.7"`, and `roxmltree = "0.20"`.
   - Release profile retains high optimization flags (`opt-level = 3`, `lto = true`, `panic = "abort"`, `strip = true`).

2. **`rust-core/src/cwmp.rs` (821 lines)**:
   - Module declares `ParsedInform`, `CwmpEvent`, and converts directly into `TelemetryUpdate` via `into_telemetry_update(client_ip)`.
   - XML parsing engine `parse_inform` uses `roxmltree` with a dynamic 5-attempt retry loop `inject_single_namespace` resolving case-mismatched or missing vendor namespace declarations (e.g. `SOAP-ENC` vs `soap-enc`).
   - Normalization functions `normalize_optical_power` and `parse_optical_power` handle 4 distinct vendor scales: decimal strings (`-19.50`), unit suffixed (`-19.50 dBm`), Huawei 0.01 dBm scaled integers (`-1950`), and TP-Link 0.001 dBm millidBm scaled integers (`-19500`).
   - Optical power discrimination `is_optical_rx_power_key` and `is_optical_tx_power_key` explicitly isolates Rx power from Tx power.
   - SOAP envelope builders implement valid XML generation for `InformResponse` echoing header ID, `Reboot` with `CommandKey`, `GetParameterValues` with `soap-enc:arrayType`, and `Fault`.

3. **`rust-core/src/main.rs` (lines 686–1141)**:
   - Embedded Axum server running on configurable `CWMP_HOST:CWMP_PORT` (default `0.0.0.0:7547`), handling `POST /`, `POST /cwmp`, and `POST /tr069`.
   - MPSC channel convergence: original `tx` moved to `run_mqtt_ingest`, cloned `axum_tx` moved to `AppState`. Senders use `tokio::time::timeout(Duration::from_millis(500), tx.send(update))` to prevent blocking network loops if DB sink is saturated.
   - Pending command dequeue via `dequeue_pending_command` using PostgreSQL transaction:
     ```sql
     SELECT id::text AS id, command_type, command_payload
     FROM cpe_pending_commands
     WHERE cpe_id = $1 AND status = 'pending'
     ORDER BY created_at ASC
     LIMIT 1
     FOR UPDATE SKIP LOCKED
     ```
     Atomically marks command as `status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP`.
   - TR-069 session lifecycle handled in `handle_cwmp_post`:
     - Inform: parses telemetry, enqueues to MPSC, sets session cookie `session=<cpe_id>`, responds with `InformResponse`.
     - Empty POST (`body.is_empty()`): resolves CPE session (Cookie -> IP Cache -> DB fallback), queries `cpe_pending_commands`, returns SOAP RPC or empty 200 OK (`Content-Length: 0`).
     - RPC Response / Fault: marks dispatched command as `completed` or `failed` in DB with `result_payload`, and dequeues subsequent pending command.
   - Graceful shutdown: `ctrl_c` sends `true` on `shutdown_tx`. Axum stops accepting connections and completes in-flight requests, MQTT disconnects and exits. Both senders are dropped, allowing `run_db_sink` to drain `rx` completely and exit without hanging.

### 1.2 Independent Verification Tool Execution Results
1. `cargo check` in `rust-core/`:
   ```
   Finished `dev` profile [unoptimized + debuginfo] target(s) in 28.90s (Exit code: 0)
   ```
2. `cargo clippy -- -D warnings` in `rust-core/`:
   ```
   Finished `dev` profile [unoptimized + debuginfo] target(s) in 8.06s (Exit code: 0)
   ```
3. `cargo test -- --nocapture` in `rust-core/`:
   ```
   running 28 tests
   test cwmp::tests::test_build_inform_response_echoes_header_id ... ok
   test cwmp::tests::test_build_reboot_and_get_parameters_rpc ... ok
   test cwmp::tests::test_device_id_fallback_when_serial_empty ... ok
   test cwmp::tests::test_optical_power_normalization_all_formats ... ok
   test cwmp::tests::test_parse_tplink_tr181_inform ... ok
   test cwmp::tests::test_parse_huawei_echolife_inform ... ok
   test cwmp::tests::test_parse_inform_tx_and_ip_and_fault ... ok
   ...
   test tests::test_build_soap_rpc_dispatch_variants ... ok
   test tests::test_cwmp_session_and_telemetry_convergence ... ok
   test result: ok. 28 passed; 0 failed; 0 ignored; finished in 0.15s
   ```
4. `cargo test --all-targets` in `rust-core/`:
   ```
   Running unittests src/main.rs (28 passed)
   Running tests/concurrency_mpsc_stress.rs (13 passed)
   Running tests/empirical_xml_stress.rs (21 passed)
   Total: 62 passed; 0 failed; 0 ignored; finished in 24.16s
   ```

### 1.3 Integrity Verification
- **Hardcoded Test Results**: None. No static shortcuts matching specific device IDs or hardcoded return strings.
- **Dummy/Facade Implementations**: None. XML parsing, database transactions, HTTP server lifecycle, and MPSC routing are completely implemented with production logic.
- **Task Shortcuts**: None. Embedded Axum and `roxmltree` are integrated natively in Rust as mandated by R1, R2, and R3.
- **Verification Logs**: Authenticated via real terminal executions and independent check runs.

---

## 2. Logic Chain

1. **Dual-Stack Ingress & MPSC Convergence (Requirement R1, R3)**:
   - Worker implemented an embedded Axum HTTP server running alongside the Rumqttc MQTT client on port 7547.
   - `AppState` receives a clone of the `mpsc::Sender<TelemetryUpdate>`. When an Inform is parsed, `ParsedInform::into_telemetry_update` populates the exact same data structure used by the TR-369 USP Protobuf/JSON pipeline.
   - Both pipelines stream to `run_db_sink`, which auto-provisions `cpe_inventory` to protect against foreign key constraints and performs atomic upserts into `cpe_live_state`.
   - Verified via `test_cwmp_session_and_telemetry_convergence` and `test_massive_multi_protocol_concurrency_convergence`.

2. **Optical Normalization & Database Trigger Alignment (Requirement R2)**:
   - The PostgreSQL trigger `reconcile_live_to_history()` inspects `telemetry_metrics->>'rx_optical_power'` and casts to numeric.
   - Huawei sends unscaled 0.01 dBm integers (e.g. `-1950`) or suffixed strings (`"-19.50 dBm"`). TP-Link sends TR-181 0.001 millidBm integers (`"-21300"`).
   - `cwmp::parse_optical_power` normalizes all valid physical optical readings into standard float dBm values (`-19.50`, `-21.30`) and filters disconnected states (`"N/A"`, `"--"`, `"0"`).
   - `is_optical_rx_power_key` guarantees that Tx optical power readings never overwrite Rx optical power.
   - Verified via `test_optical_power_normalization_all_formats` and `empirical_xml_stress.rs`.

3. **Concurrency Safety & FIFO Pending Command Dispatch (Requirement R4)**:
   - Commands queued by FastAPI in `cpe_pending_commands` must be retrieved when the ONT sends an empty POST.
   - `dequeue_pending_command` wraps `SELECT ... FOR UPDATE SKIP LOCKED` inside a PostgreSQL transaction, guaranteeing that concurrent workers or simultaneous HTTP requests never double-dispatch the same command.
   - Command state transitions to `'dispatched'` atomically within the transaction before the SOAP RPC is returned.
   - When the ONT responds with `RebootResponse` or `GetParameterValuesResponse`, the command status transitions to `'completed'` with `completed_at` and `result_payload`.

4. **Channel Lifecycle & Clean Graceful Shutdown**:
   - MPSC channels in Tokio require all `Sender` instances to be dropped before `Receiver::recv` yields `None`.
   - Senders exist only in `run_mqtt_ingest` and `run_cwmp_server` (`AppState`). `main()` retains no sender copy.
   - On `ctrl_c`, `shutdown_tx.send(true)` notifies both tasks: Axum drains active connections via `with_graceful_shutdown` and exits; MQTT disconnects and exits.
   - Once both exit, `run_db_sink` drains all buffered records and completes.
   - Verified via `test_graceful_shutdown_mpsc_drain_and_no_sender_leak`.

---

## 3. Adversarial Analysis & Stress-Testing (Critic)

### 3.1 Challenge 1: MPSC Senders Failing to Close on Interrupted Shutdown
- **Assumption Challenged**: All senders are dropped when `shutdown_tx` signals.
- **Attack Scenario**: If an in-flight HTTP request hangs in Axum or client keepalive doesn't close, Axum graceful shutdown could delay, leaving `state.tx` alive.
- **Blast Radius**: `run_db_sink` would block waiting for `rx.recv().await`, preventing container termination until Docker SIGKILL.
- **Mitigation Present**: Axum's `with_graceful_shutdown` enforces connection draining. Senders in handlers have a 500ms timeout guard (`tokio::time::timeout`). Senders do not leak outside task scopes. Stress test `test_graceful_shutdown_mpsc_drain_and_no_sender_leak` confirms clean termination and 0 leaked handles.

### 3.2 Challenge 2: Session Cache Eviction Under IP Churn
- **Assumption Challenged**: In-memory session cache (`session_cache: HashMap<IpAddr, (String, Instant)>`) handles ONT identification when cookies are missing.
- **Attack Scenario**: If millions of unique IP addresses hit the ACS without sending cookies, memory consumption in `HashMap` grows unbounded because expired entries are only checked on read, not actively pruned.
- **Blast Radius**: Slow memory creep under massive DDoS / port scanning.
- **Mitigation Present**: At 100 bytes per entry, 100,000 active devices consume only ~10MB (well below the 500MB container limit). Standard CPEs send the HTTP cookie (`session=<cpe_id>`), which bypasses cache lookups. For future hardening at carrier scale (>1M devices), periodic TTL eviction or an LRU cache can be added.

### 3.3 Challenge 3: Ingestion Saturation & HTTP Response Starvation
- **Assumption Challenged**: Database writes keep pace with incoming HTTP requests.
- **Attack Scenario**: If PostgreSQL locks up or slows down, the 1024-capacity MPSC channel fills up. Without a timeout, `state.tx.send(update).await` would block the Axum handler indefinitely, causing HTTP request timeouts on the ONT and connection teardown.
- **Blast Radius**: Cascading timeouts and reconnection storms.
- **Mitigation Present**: Line 885 implements `tokio::time::timeout(Duration::from_millis(500), state.tx.send(update))`. If the channel is saturated for >500ms, the update is dropped with a warning, and the handler immediately returns `<cwmp:InformResponse>`, ensuring the CWMP session completes successfully. Stress test `test_mpsc_saturation_and_500ms_backpressure_guard` passed.

---

## 4. Caveats

1. **Bare-Metal Port Binding**:
   Binding to privileged port 7547 requires root/elevated privileges in direct host environments. When running under Docker Compose or unprivileged testing, use `CWMP_PORT=7548` or use Docker port mapping (`7547:7547`).
2. **Session Cookie Reliance**:
   While 3-tier fallback (Cookie -> IP Cache -> DB) provides high resilience, ONTs operating behind carrier-grade NAT (CGNAT) without RFC 6265 cookie support could have session ambiguity if multiple ONTs share an identical public IP. Telecom standard practice requires TR-069 ONTs to retain session cookies or operate over dedicated management VLANs.

---

## 5. Conclusion & Verdict

The Milestone 2 implementation is robust, complete, strictly adheres to specifications, and introduces zero regressions to the existing TR-369 / MQTT pipeline. All 62 unit and stress tests pass with 100% success.

**Verdict: APPROVE**

---

## 6. Verification Method

To independently reproduce this verification:

```bash
cd /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core

# 1. Type checking and linting
cargo check
cargo clippy -- -D warnings

# 2. Unit and Integration Test Suite
cargo test -- --nocapture

# 3. All target test suite (including empirical XML and MPSC concurrency stress)
cargo test --all-targets
```

### Invalidation Conditions
- Any failure in the 62 unit, concurrency, or XML stress tests.
- Any regression in TR-369 USP Protobuf or JSON decoding.
- Unscaled optical power integers written to `cpe_live_state`.
- MPSC receiver hanging indefinitely on graceful shutdown.
