# Dispatch for explorer_m4_rust_1

## Mission
Investigate `rust-core/` to determine the exact architecture, current Tokio runtime loop, MPSC channel design, message data structures, SQLx database write pipeline, dependencies in `Cargo.toml`, and how to embed an Axum (or actix-web) HTTP server listening on port 7547 alongside the existing MQTT client without blocking or regression.

## Inputs
- Mandatory: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (specifically `## Follow-up — 2026-09-07T14:20:33Z`)
- Source: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/`
- Existing handoffs: `.agents/worker_m3_rust/handoff.md` and `.agents/orchestrator_3/handoff.md`

## Required Output
Write your findings to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_rust_1/handoff.md` including:
1. Detailed analysis of `rust-core/src/main.rs`, `Cargo.toml`, and internal MPSC channel types.
2. How the MPSC receiver currently writes to PostgreSQL (`cpe_live_state`).
3. Concrete integration plan for Axum HTTP server on port 7547 in Tokio runtime.
4. Data model changes needed for homogenized MPSC messages (TR-369 protobuf/JSON + TR-069 XML parsed data).
5. Fast XML parsing library recommendation (`roxmltree` vs `quick-xml`) with crate dependencies.
6. Verification commands and layout compliance.

## 2026-09-07T14:24:29Z
Investigate rust-core/ in detail:
- Examine Cargo.toml, build.rs, proto/, and src/main.rs.
- Analyze the current Tokio async runtime loop, the MQTT subscriber loop, the internal MPSC channel types and message definitions, and how the MPSC receiver batches or writes messages to PostgreSQL (cpe_live_state).
- Determine how to embed an Axum (or actix-web) HTTP server listening on port 7547 alongside the existing MQTT client without blocking the Tokio runtime.
- Determine how TR-069 XML Inform events can be parsed and converged into the SAME MPSC queue feeding PostgreSQL, producing unified/homogenized cpe_live_state records.
- Evaluate fast XML parsing crates (roxmltree vs quick-xml) and required dependencies.
- Write your comprehensive report to /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_rust_1/handoff.md.
- Send a completion message back when done referencing the handoff path.
