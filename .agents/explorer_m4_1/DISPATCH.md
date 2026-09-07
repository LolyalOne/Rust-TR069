## 2026-09-07T06:47:36Z
You are explorer_m4_1 (teamwork_preview_explorer).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_1

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

TASK:
Investigate and design the complete Milestone 4 Python FastAPI Manager (`python-api/`):
1. Review `simulate_flow.sh` requirements for all HTTP endpoints:
   - `POST /api/v1/cpes`: create CPE in `cpe_inventory`, return 201/200 with `cpe_id`.
   - `GET /api/v1/cpes`: list CPEs.
   - `GET /api/v1/cpes/{cpe_id}`: get CPE details (must return `model: "Archer-AX50"`, etc.).
   - `DELETE /api/v1/cpes/{cpe_id}`: delete CPE.
   - `GET /api/v1/cpes/{cpe_id}/live-state` (and alias `/live`): query `cpe_live_state` from RAM tmpfs, returning `status` and `telemetry_metrics` (`cpu_usage`, `rx_optical_power`, etc.).
   - `GET /api/v1/cpes/{cpe_id}/history`: query `cpe_historical_metrics` (or view `cpe_state_history`), returning items array.
   - `POST /api/v1/cpes/{cpe_id}/reboot`: publish TR-369 Reboot Operate command to Mosquitto topic `usp/endpoint/{cpe_id}/request` with QoS 1.
   - `GET /health`: return 200 OK for Docker healthcheck (`test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]`).
2. Design database models and async SQLAlchemy 2.0 session engine:
   - Connect to `postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db`.
   - Map `cpe_inventory`, `cpe_live_state`, and `cpe_historical_metrics`.
3. Design MQTT client integration (`aiomqtt` or `paho-mqtt` async):
   - Connect to host `mosquitto:1883`.
   - Publish command to `usp/endpoint/{cpe_id}/request`.
4. Design `python-api/gunicorn_conf.py` (2 workers, bind `0.0.0.0:8000`, `worker_class="uvicorn.workers.UvicornWorker"`).
5. Design `python-api/Dockerfile` (Python 3.11-slim) and `python-api/requirements.txt`.

Deliver a concrete implementation specification in `handoff.md` and message the orchestrator.
