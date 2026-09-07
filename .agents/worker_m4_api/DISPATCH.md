## 2026-09-07T06:50:19Z
You are worker_m4_api (teamwork_preview_worker).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m4_api

DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m4_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/postgres/init.sql
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/docker-compose.yml
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

EXCLUSIVE FILE OWNERSHIP:
You own and will create:
1. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/requirements.txt
2. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/Dockerfile
3. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/gunicorn_conf.py
4. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/__init__.py
5. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/config.py
6. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/database.py
7. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/models.py
8. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/schemas.py
9. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/mqtt.py
10. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/routers/__init__.py
11. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/routers/health.py
12. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/routers/cpes.py
13. /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/main.py

IMPLEMENTATION REQUIREMENTS (per .agents/explorer_m4_1/handoff.md):
1. `requirements.txt`:
   - `fastapi`, `uvicorn[standard]`, `gunicorn`, `pydantic`, `pydantic-settings`, `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `aiomqtt`, `paho-mqtt`, `httpx`, `pytest`, `pytest-asyncio`.
2. `gunicorn_conf.py`:
   - `bind = "0.0.0.0:8000"`, `workers = 2`, `worker_class = "uvicorn.workers.UvicornWorker"`, `timeout = 60`, `max_requests = 1000`.
3. `app/database.py` & `app/models.py`:
   - Async SQLAlchemy 2.0 with `create_async_engine`, `async_sessionmaker`.
   - Models: `CpeInventory` (table `cpe_inventory`), `CpeLiveState` (table `cpe_live_state`), `CpeHistoricalMetrics` (table `cpe_historical_metrics`).
4. `app/schemas.py`:
   - Pydantic V2 models.
   - `CpeLiveStateResponse` must include `telemetry_metrics` (with alias `metrics`), `current_parameters` (with alias `parameters`), `status`.
   - `CpeHistoryResponse` must include `cpe_id`, `total`, and `items: list[CpeHistoryItem]`.
5. `app/mqtt.py`:
   - Async MQTT publisher (using `aiomqtt` or async client) publishing TR-369 Reboot Operate command to `usp/endpoint/{cpe_id}/request` with QoS 1.
   - The command payload must contain `"command": "Device.Reboot()"` and `"operate": {"command": "Device.Reboot()", ...}` matching `simulate_flow.sh` regex `reboot|operate`.
6. `app/routers/cpes.py`:
   - `POST /api/v1/cpes`: registers CPE, handles upsert/auto-discovered row, returns 201/200 with `cpe_id`.
   - `GET /api/v1/cpes`: lists all registered CPEs.
   - `GET /api/v1/cpes/{cpe_id}`: returns CPE inventory details (`model: "Archer-AX50"`).
   - `DELETE /api/v1/cpes/{cpe_id}`: deletes CPE (cascades to live state and history).
   - `GET /api/v1/cpes/{cpe_id}/live-state` (and `/live`): queries `cpe_live_state` from RAM table.
   - `GET /api/v1/cpes/{cpe_id}/history`: queries `cpe_historical_metrics` returning items array.
   - `POST /api/v1/cpes/{cpe_id}/reboot`: publishes command to MQTT topic `usp/endpoint/{cpe_id}/request` with QoS 1.
7. `app/routers/health.py`:
   - `GET /health`: checks DB and returns 200 OK.
8. `app/main.py`:
   - FastAPI application mounting health router and cpes router at `/api/v1`.
9. `Dockerfile`:
   - Python 3.11-slim, install requirements, Gunicorn with 2 uvicorn workers.

VERIFICATION:
- Verify Python syntax across all files: `python3 -m py_compile python-api/gunicorn_conf.py python-api/app/*.py python-api/app/routers/*.py`.
- Write unit tests in `python-api/tests/test_api.py` or standalone test script to verify routes, schema serialization, and MQTT command formatting.
- Deliver your report in `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m4_api/handoff.md` and message the orchestrator.
