# Task Assignment: Worker (Milestone 2 Implementation)

## Mission
Implement Milestone 2 of the Rust-TR069 Dual-Stack Refactoring:
1. R1: Embedded Axum HTTP server on port 7547 in `rust-core`.
2. R2: XML/SOAP Parsing (`roxmltree`) for TR-069 Inform packets and Optical Telemetry Normalization in `rust-core/src/cwmp.rs`.
3. R3: MPSC channel convergence to `run_db_sink` and pending command delivery from PostgreSQL `cpe_pending_commands`.

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Owned Files
- `rust-core/Cargo.toml`
- `rust-core/src/cwmp.rs`
- `rust-core/src/main.rs`
- `rust-core/tests/` (unit and integration tests)

## Key Technical Specifications & Guide
Read the handoff reports from the 3 Explorers:
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_1/handoff.md` (Axum server, graceful shutdown, Cargo dependencies)
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_2/handoff.md` (XML parser, `cwmp.rs` full reference implementation, optical normalizer, unit tests)
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_3/handoff.md` (MPSC convergence, `cpe_pending_commands` query with `FOR UPDATE SKIP LOCKED`, TR-069 session lifecycle)

Also read:
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md`

## Verification Requirements
1. Run `cargo check` and `cargo test` in `rust-core/`. Ensure 100% pass rate with zero errors or regressions.
2. Verify all existing tests pass and new unit tests for XML Inform parsing, optical power normalization, InformResponse generation, and pending command queries pass.
3. Document all build and test commands and their full output in your handoff report.

Write your report to:
`/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m2_5/handoff.md`

## 2026-09-07T19:20:14Z
You are Worker for Milestone 2 of the Rust-TR069 Dual-Stack Refactoring.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m2_5

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

MANDATORY: Read /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md before starting work.
Also read:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m2_5/DISPATCH.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_1/handoff.md (Axum server architecture & dependencies)
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_2/handoff.md (Full roxmltree parser implementation & normalization tests)
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_3/handoff.md (MPSC convergence & cpe_pending_commands query with SKIP LOCKED)

Your Owned Files:
- rust-core/Cargo.toml (add axum = "0.7" and roxmltree = "0.20")
- rust-core/src/cwmp.rs (create CWMP module with XML parsing, normalization, SOAP responses)
- rust-core/src/main.rs (integrate Axum HTTP server on port 7547 alongside MQTT, clone MPSC tx, graceful shutdown)
- rust-core/tests/ (or unit tests in src/cwmp.rs)

Your Tasks:
1. Update `rust-core/Cargo.toml` with `axum = "0.7"` and `roxmltree = "0.20"`.
2. Create `rust-core/src/cwmp.rs` implementing:
   - `parse_inform(xml: &str) -> Result<ParsedInform, ...>` extracting DeviceId (SerialNumber, Manufacturer, OUI, ProductClass), events, parameter map.
   - `normalize_optical_power(key, val)` normalizing Huawei TR-098 and TP-Link TR-181 optical power to standard float dBm (e.g. -19.50) without unit strings or scaled integers.
   - `generate_inform_response(id: &str) -> String` producing standard `<cwmp:InformResponse>`.
   - `generate_reboot_rpc(id: &str, command_key: &str) -> String` and `generate_get_parameter_values_rpc(id: &str, names: &[String]) -> String`.
3. Modify `rust-core/src/main.rs`:
   - Declare `mod cwmp;`.
   - In `main()`: clone `let axum_tx = tx.clone();`, read `CWMP_PORT` (default 7547) and `CWMP_HOST` (default 0.0.0.0).
   - Build Axum router with `POST /`, `POST /cwmp`, `POST /tr069`.
   - Shared `AppState { tx: mpsc::Sender<TelemetryUpdate>, db_pool: PgPool }`.
   - On incoming POST:
     - If empty body (Content-Length 0 or empty): query `cpe_pending_commands` where `cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`, update to `'dispatched'`, return SOAP RPC. If no commands, return empty 200 OK.
     - If Inform: parse XML, construct `TelemetryUpdate`, send via `state.tx.send(update)` with 500ms timeout, return HTTP 200 OK with `generate_inform_response(id)` and `Set-Cookie: session=<cpe_id>; Path=/; HttpOnly`.
     - If RPC Response: update command status to `'completed'` with `completed_at` and `result_payload`.
   - Spawn Axum server with graceful shutdown hooked to `shutdown_rx`.
   - Ensure original `tx` is dropped in `main` so `run_db_sink` drains cleanly.
4. Run tests:
   - Execute `cargo test` in `rust-core/` and verify that all tests (both existing and new CWMP tests) pass.
5. Write handoff report with verification outputs to:
   `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m2_5/handoff.md`
   and notify orchestrator when done.
