# BRIEFING — 2026-09-07T06:38:45Z

## Mission
Build the rust-core high-throughput USP (TR-369) ingestion engine and protobuf definition, handling both JSON and Protobuf payloads, persisting to PostgreSQL and running in Docker.

## 🔒 My Identity
- Archetype: teamwork_preview_worker
- Roles: implementer, qa, specialist
- Working directory: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m3_rust
- Original parent: 72558cd4-b522-4129-816f-63bb0c581dfa
- Milestone: M3 Rust Core Engine

## 🔒 Key Constraints
- DO NOT CHEAT. All implementations must be genuine.
- Exclusive file ownership:
  1. rust-core/Cargo.toml
  2. rust-core/build.rs
  3. rust-core/proto/usp.proto
  4. rust-core/src/main.rs
  5. rust-core/Dockerfile
- Dual payload decoder:
  * JSON: cpe_id, status, metrics, parameters
  * Binary: usp::Record -> usp::Msg -> endpoint_id, params, events
- MPSC channel (tokio::sync::mpsc::channel(1024)) decoupling MQTT ingest from DB writer
- MQTT loop: subscribes to usp/endpoint/#, ignores topics ending in /request, handles reconnects
- DB loop: connects to DATABASE_URL with retry, pre-provisions cpe_inventory (ON CONFLICT (cpe_id) DO NOTHING), atomic UPSERT into cpe_live_state merging JSONB metrics and parameters via ||
- Healthcheck task: writes/touches /tmp/healthy periodically
- Multi-stage Dockerfile (rust:alpine / musl -> alpine) running < 500MB RAM

## Current Parent
- Conversation ID: 72558cd4-b522-4129-816f-63bb0c581dfa
- Updated: 2026-09-07T06:38:45Z

## Task Summary
- **What to build**: Complete `rust-core/` module with TR-369 Protobuf definition, dual payload decoder (JSON and BBF Protobuf), Tokio MPSC pipeline, Rumqttc eventloop with exponential backoff, PostgreSQL atomic UPSERT with JSONB concatenation, periodic healthcheck task touching `/tmp/healthy`, and multi-stage Alpine Dockerfile.
- **Success criteria**: 100% genuine code, zero compilation warnings, clean clippy, 5/5 passing unit tests for JSON and Protobuf payloads, stripped release binary of 3.9MB.
- **Interface contracts**: `postgres/init.sql`, `docker-compose.yml`, `simulate_flow.sh`, Broadband Forum TR-369 1.3 specification.
- **Code layout**: `rust-core/`

## Key Decisions Made
- Used self-contained, BBF tag-matched `usp.proto` ensuring 100% wire-compatibility with standard TR-369 agents while eliminating fragile multi-file imports.
- Dual payload decoder inspects first non-whitespace byte to prioritize JSON fast-path or Protobuf fast-path, with automatic cross-fallback.
- Normalized optical power into `telemetry_metrics.rx_optical_power` to ensure compatibility with PostgreSQL `reconcile_live_to_history()` trigger.
- Auto-provisions `cpe_inventory` stub on telemetry arrival to avoid FK 23503 errors.
- Bounded MPSC enqueue timeout of 500ms ensures database stalls never drop MQTT PINGREQ/PINGRESP keepalive frames.

## Artifact Index
- `rust-core/Cargo.toml` — Crate dependencies and build configuration.
- `rust-core/build.rs` — Protobuf code generator using `prost-build`.
- `rust-core/proto/usp.proto` — BBF TR-369 1.3 wire-compatible Protobuf schema.
- `rust-core/src/main.rs` — Complete Rust USP Core worker implementation with tests.
- `rust-core/Dockerfile` — Multi-stage Alpine production Dockerfile.
- `.agents/worker_m3_rust/handoff.md` — 5-component handoff report.

## Change Tracker
- **Files modified**:
  * `rust-core/Cargo.toml`: Initialized with tokio, rumqttc, sqlx, prost, serde, chrono, tracing, etc.
  * `rust-core/build.rs`: Compiles `proto/usp.proto` via prost_build.
  * `rust-core/proto/usp.proto`: TR-369 1.3 wire-compatible schema.
  * `rust-core/src/main.rs`: Full worker implementation, dual decoder, MPSC channel, health monitor, 5 unit tests.
  * `rust-core/Dockerfile`: Multi-stage Alpine musl build.
- **Build status**: `cargo check`, `cargo clippy`, `cargo test`, and `cargo build --release` PASS (0 errors, 0 warnings).
- **Pending issues**: None.

## Quality Status
- **Build/test result**: 5 passed, 0 failed, 0 ignored in 0.01s (both debug and release).
- **Lint status**: 0 warnings in `cargo check` and `cargo clippy`.
- **Tests added/modified**:
  * `test_topic_filtering_and_extraction`
  * `test_decode_json_step2_payload`
  * `test_decode_json_step4_altered_payload`
  * `test_decode_protobuf_wire_compatible_record`
  * `test_decode_protobuf_event_notification`

## Loaded Skills
None
