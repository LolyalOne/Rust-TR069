## 2026-09-07T06:22:22Z
You are worker_m3_rust (teamwork_preview_worker).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m3_rust

DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_usp_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m3_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m3_2/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m3_3/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

EXCLUSIVE FILE OWNERSHIP:
You own and will create:
1. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/Cargo.toml
2. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/build.rs
3. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/proto/usp.proto
4. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/src/main.rs
5. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/Dockerfile

IMPLEMENTATION SPECIFICATION:
1. `rust-core/proto/usp.proto`:
   - Full TR-369 1.3 BBF-wire-compatible Protobuf definition as detailed in `.agents/spec_miner_usp_1/handoff.md` and `.agents/explorer_m3_1/handoff.md`:
     `Record`, `NoSessionContextRecord`, `Msg`, `Header`, `Body`, `Request`, `Response`, `Notify`, `Operate`, `Get`, `Set`, `Error`.
2. `rust-core/Cargo.toml`:
   - Dependencies: `tokio` (features = ["full"]), `rumqttc = "0.24"`, `sqlx` (version = "0.7", default-features = false, features = ["runtime-tokio-rustls", "postgres", "json", "chrono"]), `prost = "0.12"`, `prost-types = "0.12"`, `serde` (features = ["derive"]), `serde_json = "1.0"`, `chrono` (features = ["serde"]), `tracing = "0.1"`, `tracing-subscriber` (features = ["env-filter"]), `anyhow = "1.0"`.
   - Build-dependencies: `prost-build = "0.12"`.
3. `rust-core/build.rs`:
   - Compiles `proto/usp.proto` using `prost_build`.
4. `rust-core/src/main.rs`:
   - Complete, genuine implementation.
   - Dual payload decoder:
     * Checks if payload is JSON (starts with `{` or parses via serde_json): extracts `cpe_id`, `status`, `metrics`, `parameters`.
     * If binary: decodes `usp::Record` -> `usp::Msg` -> extracts `endpoint_id`, `params`, `events`.
   - MPSC channel (`tokio::sync::mpsc::channel(1024)`) decoupling MQTT ingest from DB writer.
   - MQTT loop: subscribes to `usp/endpoint/#`, ignores topics ending in `/request`, handles reconnects.
   - DB loop: connects to `DATABASE_URL` with retry, ensures inventory pre-provisioning (`ON CONFLICT (cpe_id) DO NOTHING`), performs atomic UPSERT into `cpe_live_state` merging JSONB metrics and parameters via `||`.
   - Healthcheck task: writes/touches `/tmp/healthy` periodically when alive (`docker-compose.yml:65`).
5. `rust-core/Dockerfile`:
   - Multi-stage build (`rust:1.77-alpine` or `rust:alpine` with `musl-dev`, `protobuf`, `protobuf-dev` -> `alpine:3.19`).
   - Ensures binary compiles and runs within 500M RAM.

VERIFICATION:
- Verify rust syntax, build script, proto formatting.
- If cargo/rustc is installed on the host, run `cargo check` inside `rust-core/` (or test compilation).
- Deliver your report to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m3_rust/handoff.md` and message the orchestrator.
