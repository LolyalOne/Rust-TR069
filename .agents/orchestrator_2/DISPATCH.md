## 2026-09-07T05:59:33Z
You are the Project Orchestrator for the ACS TR-369/USP project (Rust-TR069).

Project root directory: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069
Your working directory: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2

Authoritative reference files:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/HANDOVER_STATUS.md (Exact status of all milestones and roadmap)
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md (User requests and requirements)
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_usp_1/handoff.md (Detailed USP/TR-369 protobuf specifications, topic hierarchy, tokio mpsc architecture)
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_1/ (Previous orchestrator documentation and test infra)

Tasks to execute:
1. Finalize Milestone 2 (PostgreSQL):
   - Fix `postgres/init.sql` so that `CREATE TABLESPACE` is NOT executed inside transactional blocks (`DO $$`).
   - Fix reconciliation trigger `reconcile_live_to_history` so it triggers strictly when optical signal variation is > 1.0 dBm, inserting into `cpe_historical_metrics` without performing an `UPDATE` on `cpe_inventory`.
   - Validate using `postgres/test_schema.py` and `postgres/test_reconciliation_empirical.py`.
2. Implement Milestone 3 (Rust USP Core Worker in `rust-core/`):
   - Implement according to `.agents/spec_miner_usp_1/handoff.md` and `HANDOVER_STATUS.md`.
   - Tokio async runtime, MPSC channel, SQLx, rumqttc, prost Protobuf decoding from `usp/endpoint/#`.
   - Create `rust-core/Cargo.toml`, `build.rs`, `proto/usp.proto`, `src/main.rs`, and multi-stage `Dockerfile`.
3. Implement Milestone 4 (Python FastAPI Manager in `python-api/`):
   - Python 3.11, FastAPI, async SQLAlchemy 2.0, Gunicorn (2 workers) for memory containment.
   - Endpoints for CPE inventory, real-time live state, command injection into MQTT.
   - Create `requirements.txt`, `app/main.py`, `gunicorn_conf.py`, and `Dockerfile`.
4. Update `README.md`:
   - Reflect completion of Milestones 3 & 4 in roadmap, finalize all sections to reflect a ready-to-run system.
5. Verification & Acceptance:
   - Verify `docker compose up -d --build` builds and starts all services cleanly.
   - Run `./simulate_flow.sh` and ensure it completes with 100% success (exit code 0).
6. Operational Protocol:
   - Maintain `progress.md` and `BRIEFING.md` in `.agents/orchestrator_2/`.
   - When everything is completed and verified, notify the Sentinel with a full summary.
