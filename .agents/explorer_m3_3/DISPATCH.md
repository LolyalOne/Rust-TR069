## 2026-09-07T06:17:36Z
You are explorer_m3_3 (teamwork_preview_explorer).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m3_3

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

TASK:
Investigate and design the PostgreSQL Database Sink and Multi-Stage Dockerfile for Milestone 3 (`rust-core/`):
1. Design the SQLx database pool and asynchronous upsert query:
   - Target table: `cpe_live_state` in PostgreSQL (with columns: `cpe_id`, `endpoint_id`, `current_parameters`, `telemetry_metrics`, `status`, `ip_address`, `firmware_version`, `last_seen`, `updated_at`).
   - Must handle `ON CONFLICT (cpe_id) DO UPDATE` merging `current_parameters` and `telemetry_metrics` via JSONB concatenation (`||`), updating `status` and `last_seen`.
2. Design the multi-stage `Dockerfile`:
   - Builder stage: Alpine-based (`rust:alpine` or `rust:1.77-alpine`) with required packages (`musl-dev`, `protobuf`, `protobuf-dev`).
   - Runner stage: Minimal `alpine:3.19` (or `latest`) with `libgcc`, non-root user or standard app user.
   - Ensure memory usage stays well within the 500M Docker limit.
3. Verify local buildability and testability of `rust-core` crate (e.g. `cargo check` / `cargo test` on host if cargo is available).

Deliver a concrete implementation specification in `handoff.md` and message the orchestrator.
