# Task Assignment: Explorer 3 (MPSC Convergence, DB Ingestion & Command Delivery)

## Context
Milestone 2 of Rust-TR069 Dual-Stack Refactoring: MPSC Channel Convergence and Pending Command Delivery.
Target components: `rust-core/src/main.rs`, PostgreSQL integration (`sqlx::PgPool`, `cpe_pending_commands`).

## Objectives
1. Inspect `rust-core/src/main.rs` to see how `tokio::sync::mpsc::channel::<TelemetryUpdate>(1024)` is instantiated and how `run_db_sink` consumes it:
   - Verify if `tx: tokio::sync::mpsc::Sender<TelemetryUpdate>` can be cloned into Axum route handler state.
   - Verify how `TelemetryUpdate` is constructed from CWMP Inform data (`cpe_id = SerialNumber`, `status = "online"`, `current_parameters = json!(...)`, `telemetry_metrics = json!({"rx_optical_power": ...})`, `firmware_version`, etc.).
   - Verify that when `run_db_sink` ingests this `TelemetryUpdate`, it properly auto-provisions `cpe_inventory` and updates `cpe_live_state`.
2. Inspect the database schema for `cpe_pending_commands` (`postgres/init.sql`):
   - Analyze how to query pending commands using `sqlx`:
     `SELECT id, command_type, command_payload FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`
   - How to transition status to `'dispatched'`:
     `UPDATE cpe_pending_commands SET status = 'dispatched', dispatched_at = CURRENT_TIMESTAMP WHERE id = $1`
3. Detail how the CWMP HTTP handler should handle:
   - An incoming `cwmp:Inform` POST: parse XML -> build `TelemetryUpdate` -> `tx.send(update).await` -> query pending commands (or queue for empty POST) -> respond with `cwmp:InformResponse` (or command if immediately piggybacked/afterwards).
   - An incoming Empty HTTP POST (`Content-Length: 0` or empty body): ONT signaling completion of its requests -> query pending commands from `cpe_pending_commands` -> if found, return SOAP RPC (`cwmp:Reboot` or `cwmp:GetParameterValues`); if none found, return empty HTTP 200 OK.
   - Session tracking (e.g. cookie `Set-Cookie: session=<cpe_id>`).
4. Detail error handling (database timeout, channel backpressure, invalid payload).

## Artifacts to Read
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (MANDATORY)
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md`
- `postgres/init.sql`
- `rust-core/src/main.rs`

## Deliverables
Write your comprehensive analysis to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_3/handoff.md`.

## 2026-09-07T19:16:10Z
You are Explorer 3 for Milestone 2 of the Rust-TR069 Dual-Stack Refactoring.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_3

MANDATORY: You must read /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md before starting work.
Also read:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_3/DISPATCH.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/src/main.rs

Investigate:
1. MPSC channel convergence: how Axum handler can clone `tx: tokio::sync::mpsc::Sender<TelemetryUpdate>` and send parsed Inform data so `run_db_sink` receives it seamlessly without DB changes.
2. PostgreSQL pending commands retrieval:
   - Querying `cpe_pending_commands` using `sqlx::PgPool` (which can be passed in Axum State).
   - SQL query with SKIP LOCKED: `SELECT id, command_type, command_payload FROM cpe_pending_commands WHERE cpe_id = $1 AND status = 'pending' ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED`.
   - Updating status to 'dispatched'.
3. CWMP session handling:
   - Responding to Inform with InformResponse.
   - Handling subsequent Empty POST (`Content-Length: 0`) from ONT by returning pending command SOAP RPC (GetParameterValues, Reboot) or Empty 200 OK when queue is empty.
   - Returning commands in SOAP XML format.

Write your report to:
/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_3/handoff.md
When done, message orchestrator with a brief summary referencing the handoff path.
