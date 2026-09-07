# BRIEFING — 2026-09-07T00:58:50Z

## Mission
Probe and document precise requirements and specifications for TR-369/USP protocol over MQTT, protobuf schemas (USP Record / USP Msg), Rust USP Core worker architecture with MPSC channel decoupling, and FastAPI Manager command dispatch.

## 🔒 My Identity
- Archetype: teamwork_preview_spec_miner
- Roles: USP Protocol Spec Miner
- Working directory: /mnt/d/Projetos/TR069-181/.agents/spec_miner_usp_1
- Original parent: 6258e12c-9553-47a2-9624-69521a0b2d82
- Milestone: Phase 1 - Architecture & Specification Mining

## 🔒 Key Constraints
- Authoritative user request: /mnt/d/Projetos/TR069-181/ORIGINAL_REQUEST.md
- Specification miner is read-only: do NOT implement product code, only explore, probe, and document specifications.
- Must evaluate whether to fetch BBF official proto (`usp-record-1-3.proto`, `usp-msg-1-3.proto`) or provide self-contained mock proto conforming to TR-369 compatible with `prost`.
- Must document MQTT topic structure (`usp/endpoint/#`), Protobuf schema, Rust worker architecture (tokio, rumqttc, sqlx, prost, MPSC decoupling), and FastAPI command dispatch.
- Output handoff report to /mnt/d/Projetos/TR069-181/.agents/spec_miner_usp_1/handoff.md.

## Current Parent
- Conversation ID: 6258e12c-9553-47a2-9624-69521a0b2d82
- Updated: 2026-09-07T00:58:50Z

## Task Summary
- **What to probe**:
  1. TR-369 / USP Protocol over MQTT: Topic structure, USP Record / USP Msg schemas, fields.
  2. Rust USP Core Worker architecture: `tokio`, `rumqttc`, `sqlx`, `prost`, MPSC channel decoupling MQTT ingest from Postgres DB writer.
  3. FastAPI Manager command dispatch: Mosquitto publishing, topics, payload structure for Reboot, Set, Get.
- **Success criteria**: Comprehensive, self-contained handoff.md with Features Discovered and Edge Cases tables, ready for implementation agents.

## Key Decisions Made
- Evaluated BBF official protos (131 lines + 594 lines) vs self-contained wire-compatible schema. Recommended providing a clean self-contained `proto/usp.proto` that preserves BBF TR-369 tag numbers and wire compatibility.
- Standardized MQTT topics: `usp/endpoint/{endpoint_id}/notify` for telemetry/events and `usp/endpoint/{endpoint_id}/request` for controller commands.
- Defined Rust worker MPSC channel pattern (`tokio::sync::mpsc::channel(1024)`) decoupling `rumqttc` event loop from `sqlx` DB writer.
- Formalized PostgreSQL schemas with `cpe_live_state` as `UNLOGGED` in RAM and automatic reconciliation trigger updating `cpe_state_history` and `cpe_inventory`.
- Defined FastAPI Reboot command dispatch flow via Protobuf `Operate` message.

## Artifact Index
- handoff.md — Comprehensive TR-369/USP ACS Specification Report
- progress.md — Liveness heartbeat and activity log
