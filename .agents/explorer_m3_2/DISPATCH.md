## 2026-09-07T06:17:35Z
You are explorer_m3_2 (teamwork_preview_explorer).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m3_2

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_usp_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

TASK:
Investigate and design the Tokio Async Runtime, Rumqttc, and MPSC Pipeline for Milestone 3 (`rust-core/`):
1. Design the MPSC channel architecture (`tokio::sync::mpsc::channel(200)` or 1024) decoupling the MQTT ingest task from the PostgreSQL writer task.
2. Design MQTT subscription and topic handling:
   - Subscribe to `usp/endpoint/#`.
   - Filter out command topics (`/request`) to prevent feedback loops.
   - Handle broker disconnection and reconnect with exponential backoff.
3. Design healthcheck mechanism:
   - `docker-compose.yml` specifies `test -f /tmp/healthy || exit 1`. Ensure worker touches `/tmp/healthy` periodically or when DB and MQTT connections are live.
4. Define the `TelemetryUpdate` internal struct passed through the MPSC channel.

Deliver a concrete implementation specification in `handoff.md` and message the orchestrator.
