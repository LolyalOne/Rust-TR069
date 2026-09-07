# Dispatch for explorer_m4_api_db_1

## Mission
Investigate `python-api/`, `postgres/`, and `docker-compose.yml` to analyze how commands are issued, how `cpe_live_state` and inventory tables are structured, how pending commands for polled ONTs (CWMP TR-069) should be stored and queried (via Postgres table or HTTP direct), and how port 7547 should be exposed in Docker Compose.

## Inputs
- Mandatory: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (specifically `## Follow-up — 2026-09-07T14:20:33Z`)
- Source: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/`, `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/`, `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml`, `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh`
- Existing handoffs: `.agents/worker_m4_api/handoff.md`, `.agents/worker_m2_db/handoff.md`

## Required Output
Write your findings to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_api_db_1/handoff.md` including:
1. Current schema in PostgreSQL (`postgres/init.sql`), tables (`cpe_inventory`, `cpe_live_state`, `cpe_historical_metrics`), and triggers.
2. How `python-api` currently interacts with PostgreSQL and MQTT for commands (`GetParameterValues`, `Reboot`).
3. Optimal design for TR-069 command queuing: table in Postgres (e.g., `cpe_pending_commands` or similar) vs direct HTTP endpoint in Rust core, pros and cons, and schema requirements.
4. Docker Compose updates needed for `rust-core` port 7547 exposure.
5. Impact on existing `simulate_flow.sh` to ensure strict non-regression.

## 2026-09-07T14:24:30Z
You are explorer_m4_api_db_1.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_api_db_1
Mandatory initial read:
1. Read /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md (specifically ## Follow-up — 2026-09-07T14:20:33Z).
2. Read your dispatch instructions at /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_api_db_1/DISPATCH.md.

Task:
Investigate python-api/, postgres/, and docker-compose.yml:
- Examine postgres/init.sql and existing tables (cpe_inventory, cpe_live_state, cpe_historical_metrics).
- Examine python-api/app/main.py, models, and endpoints for issuing commands (GetParameterValues, Reboot) and querying live/historical state.
- Examine how TR-069 pending commands should be stored and queried: since TR-069 ONTs poll the ACS via HTTP Inform sessions, determine the best design for python-api to queue commands for ONTs and for Rust CWMP server to fetch and dispatch them in the HTTP response session.
- Examine docker-compose.yml: what changes are needed to expose port 7547:7547 on rust-core.
- Examine simulate_flow.sh: verify how the existing MQTT integration flow works to guarantee 0 regressions.
- Write your comprehensive report to /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_api_db_1/handoff.md.
- Send a completion message back when done referencing the handoff path.
