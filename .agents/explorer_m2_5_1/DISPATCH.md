# Task Assignment: Explorer 1 (Rust Core Axum Server & Concurrency Architecture)

## Context
Milestone 2 of Rust-TR069 Dual-Stack Refactoring: Implement Embedded HTTP (CWMP) Server in Rust Core.
Target component: `rust-core/src/main.rs`, `rust-core/Cargo.toml`.

## Objectives
1. Inspect `rust-core/Cargo.toml` and determine the exact dependencies needed for `axum` (compatible with current `tokio = 1.35+`, `tower`, `hyper`), routing, and error handling.
2. Inspect `rust-core/src/main.rs` to understand the current `tokio::main` lifecycle, signal handling, graceful shutdown, and how the MQTT client loop and `run_db_sink` are spawned (`tokio::spawn`).
3. Design the architecture for the embedded Axum HTTP server:
   - Listening on `0.0.0.0:7547` (with env var `CWMP_PORT` defaulting to 7547, `CWMP_HOST` defaulting to 0.0.0.0).
   - Routes: POST `/`, `/cwmp`, `/tr069`.
   - Axum state: sharing `mpsc::Sender<TelemetryUpdate>` and `sqlx::PgPool`.
   - Graceful shutdown integration with existing Tokio cancellation tokens or shutdown channels.
4. Verify Cargo build constraints and produce an architectural recommendation.

## Artifacts to Read
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (MANDATORY)
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md`
- `rust-core/Cargo.toml`
- `rust-core/src/main.rs`

## Deliverables
Write your comprehensive analysis to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_1/handoff.md`.

## 2026-09-07T19:16:10Z
Received dispatch for Explorer 1 for Milestone 2 of the Rust-TR069 Dual-Stack Refactoring.
Investigate:
1. Exact Cargo dependencies needed for axum, tower, hyper, etc. compatible with rust-core's current tokio version.
2. Current Tokio runtime lifecycle in rust-core/src/main.rs, MQTT subscriber spawn, run_db_sink spawn, cancellation/shutdown handling.
3. Axum server architecture: binding 0.0.0.0:7547 (via CWMP_PORT env var), endpoints POST /, /cwmp, /tr069, Axum AppState (MPSC Sender, PgPool).
4. Concrete architectural recommendation for the Worker.
