# Workspace Infrastructure & Dev Environment Survey (R1, R2, R3)

## 1. Observation

Direct observations obtained through filesystem inspection, tool execution, and source analysis across `/mnt/d/Projetos/TR069-181`:

### 1.1 `docker-compose.yml` Configuration
File inspected: `/mnt/d/Projetos/TR069-181/docker-compose.yml` (91 lines total).

1. **Services Defined**:
   - `postgres` (lines 4-26)
   - `mosquitto` (lines 27-45)
   - `rust-core` (lines 46-64)
   - `python-api` (lines 65-83)

2. **Base Images & Build Contexts**:
   - `postgres`: `image: postgres:15-alpine` (line 5)
   - `mosquitto`: `image: eclipse-mosquitto:2` (line 28)
   - `rust-core`: `build: { context: ./rust-core }` (lines 47-48)
   - `python-api`: `build: { context: ./python-api }` (lines 66-67)

3. **Memory Limits Configured (deploy.resources.limits.memory)**:
   - `postgres`: `memory: 1.5G` (line 18)
   - `mosquitto`: `memory: 500M` (line 37)
   - `rust-core`: `memory: 500M` (line 61)
   - `python-api`: `memory: 1G` (line 80)
   - **CPU Limits**: Not defined in any service (unconstrained).

4. **Volumes & tmpfs Definitions**:
   - `postgres`:
     - Volume: `pg_data:/var/lib/postgresql/data` (line 11)
     - Bind mount: `./postgres/init.sql:/docker-entrypoint-initdb.d/init.sql` (line 12)
     - tmpfs mount: `- /var/lib/postgresql/ram_data:uid=999,gid=999,mode=0700,size=1G` (lines 13-14)
   - `mosquitto`:
     - Bind mount: `./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf` (line 30)
     - Volume: `mosquitto_data:/mosquitto/data` (line 31)

5. **Port Mappings**:
   - `mosquitto`: `"1883:1883"` (line 33) — exposed to host.
   - `python-api`: `"8000:8000"` (line 73) — exposed to host.
   - `postgres`: No host port mapping; port 5432 is accessible only via Docker bridge network `acs_network`.
   - `rust-core`: No port mapping (worker daemon).

6. **Environment Variables**:
   - `postgres`: `POSTGRES_DB: acs_db`, `POSTGRES_USER: acs_user`, `POSTGRES_PASSWORD: acs_password` (lines 7-9)
   - `rust-core`: `DATABASE_URL=postgres://acs_user:acs_password@postgres:5432/acs_db`, `MQTT_HOST=mosquitto`, `MQTT_PORT=1883` (lines 50-52)
   - `python-api`: `DATABASE_URL=postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db`, `MQTT_HOST=mosquitto`, `MQTT_PORT=1883` (lines 69-71)

7. **Healthcheck Status**:
   - `postgres`: Defined (lines 19-23) with `test: ["CMD-SHELL", "pg_isready -U acs_user -d acs_db"]`, interval 5s, timeout 5s, retries 5.
   - `mosquitto`: Defined (lines 38-42) with `test: ["CMD-SHELL", "mosquitto_sub -t '$$SYS/#' -C 1 | grep -v Error || exit 1"]`, interval 10s, timeout 5s, retries 3.
   - `rust-core`: **MISSING** (lines 46-64 contain no `healthcheck` stanza).
   - `python-api`: **MISSING** (lines 65-83 contain no `healthcheck` stanza).

8. **Inter-Service Dependencies (`depends_on`)**:
   - `rust-core`: depends on `postgres` (service_healthy) and `mosquitto` (service_healthy) (lines 53-57).
   - `python-api`: depends on `postgres` (service_healthy) only (lines 74-76). It does NOT depend on `mosquitto`.

### 1.2 `.devcontainer` Configuration
File inspected: `/mnt/d/Projetos/TR069-181/.devcontainer/devcontainer.json` (22 lines total).

1. Target service: `"service": "python-api"` (line 4)
2. Compose file referenced: `"dockerComposeFile": "../docker-compose.yml"` (line 3)
3. Workspace folder: `"workspaceFolder": "/workspace"` (line 5)
4. VS Code Extensions listed (lines 8-14):
   - `"rust-lang.rust-analyzer"`
   - `"ms-python.python"`
   - `"ms-python.vscode-pylance"`
   - `"ms-azuretools.vscode-docker"`
   - `"tamasfe.even-better-toml"`
5. Settings (lines 15-18):
   - `"python.formatting.provider": "black"`
   - `"editor.formatOnSave": true`
6. Missing files:
   - No `.devcontainer/Dockerfile` exists.
   - No DevContainer Features (`features`) are declared.

### 1.3 `bootstrap.sh` and `mosquitto/` Configuration
Files inspected: `/mnt/d/Projetos/TR069-181/bootstrap.sh` and `/mnt/d/Projetos/TR069-181/mosquitto/mosquitto.conf`.

1. **`mosquitto/mosquitto.conf`**:
   - Listener: `listener 1883` (line 1)
   - Anonymous access: `allow_anonymous true` (line 2)
   - Persistence: `persistence true`, `persistence_location /mosquitto/data/`, `autosave_interval 60` (lines 3-5)
   - Logging: `log_type all` (line 6)
   - Authentication/ACL: No `password_file` or `acl_file` configured.
2. **`bootstrap.sh`**:
   - Shell script that creates directories (`.devcontainer`, `mosquitto`, `postgres`, `rust-core/src`, `rust-core/proto`, `python-api/app`).
   - Writes mock protobuf to `rust-core/proto/usp.proto`.
   - Uses `touch` to create empty files.
   - Current execution status: `bootstrap.sh` has **not** been executed. Directories `./postgres/`, `./rust-core/`, and `./python-api/` do not exist in `/mnt/d/Projetos/TR069-181`.

### 1.4 Host Environment & Tool Availability
Execution of discovery commands in WSL Ubuntu environment:
- Python 3: `/usr/bin/python3` (v3.12.3 available).
- Python libraries: `yaml` (PyYAML) and `curses` are installed and functional. `tkinter` is not installed.
- Git: `/usr/bin/git` available. Git repo is not yet initialized (`fatal: not a git repository`).
- Docker / Docker Compose / Rustc / Cargo: Not present directly in WSL `$PATH` (Docker Desktop integration or remote daemon needed for local engine execution).

---

## 2. Logic Chain

### 2.1 Memory Limits and R1 Compliance
- **Observation**: R1 mandates: Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB), Python FastAPI (1 GB).
- **Observation**: `docker-compose.yml` sets `limits.memory` to `1.5G`, `500M`, `500M`, and `1G` respectively.
- **Deduction**: The static memory limit values defined in `docker-compose.yml` strictly adhere to requirement R1.

### 2.2 PostgreSQL tmpfs Ownership Incompatibility (Critical Flaw)
- **Observation**: `docker-compose.yml` line 5 sets `image: postgres:15-alpine`.
- **Observation**: Lines 13-14 configure `tmpfs: - /var/lib/postgresql/ram_data:uid=999,gid=999,mode=0700,size=1G`.
- **Observation**: Official Docker Hub / Alpine packaging specifications for `postgres:15-alpine` assign UID 70 and GID 70 to the `postgres` system user (Debian-based postgres uses UID 999).
- **Deduction**: Because the tmpfs volume will be created with ownership `999:999` and restrictive mode `0700`, the PostgreSQL process running as UID `70` inside the container will not have write or execution permissions on `/var/lib/postgresql/ram_data`.
- **Impact**: Any attempt during `init.sql` to execute `CREATE TABLESPACE ram_ts LOCATION '/var/lib/postgresql/ram_data';` will fail with `Permission denied`, blocking initialization of the unlogged RAM tables required by R4.
- **Resolution**: Either change the tmpfs mount specification to `uid=70,gid=70,mode=0700,size=1G`, switch to Debian-based `postgres:15`, or adjust permissions to allow the container user access.

### 2.3 Acceptance Criteria Healthcheck Gap (Critical Flaw)
- **Observation**: Acceptance Criteria specifies: "A execução do `docker-compose up` levanta os 4 serviços com os limites configurados e todos atingem o status `healthy`."
- **Observation**: `docker-compose.yml` defines `healthcheck` blocks only for `postgres` and `mosquitto`. `rust-core` and `python-api` contain no `healthcheck` blocks.
- **Deduction**: Under Docker Compose semantics, services lacking a `healthcheck` definition report status `Up` (or `running`), but never transition to `healthy`. Running `docker-compose up` in the current configuration can never satisfy the acceptance criterion of all 4 services reaching `healthy`.
- **Resolution**:
  1. Add a healthcheck for `python-api`: Querying `http://localhost:8000/health` via `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"` (independent of whether `curl` is in the image).
  2. Add a healthcheck for `rust-core`: Checking a heartbeat file touched by the Tokio event loop (e.g. `test -f /tmp/healthy || exit 1`) or a lightweight healthcheck command.

### 2.4 Service Dependency for `python-api`
- **Observation**: R6 specifies that Python FastAPI handles command dispatch (e.g. Reboot) by publishing to the MQTT broker.
- **Observation**: `docker-compose.yml` lines 74-76 specify `depends_on: postgres: condition: service_healthy`, omitting `mosquitto`.
- **Deduction**: If `python-api` initializes an MQTT client during application startup, starting before Mosquitto is healthy could cause unhandled connection exceptions or restart loops.
- **Resolution**: Add `mosquitto: condition: service_healthy` to `python-api.depends_on`.

### 2.5 Mosquitto Healthcheck Reliability
- **Observation**: `docker-compose.yml` line 39 runs `mosquitto_sub -t '$$SYS/#' -C 1 | grep -v Error || exit 1` with a 5s timeout.
- **Deduction**: In Mosquitto 2.x, the default `$SYS` update interval is 10 seconds (`sys_interval 10`). If no `$SYS` topic has published when `mosquitto_sub` connects, the command blocks waiting for a message. With `timeout: 5s`, the healthcheck risks timing out and flagging Mosquitto unhealthy during initial boot.
- **Resolution**: A non-blocking probe such as `mosquitto_pub -h localhost -t 'probe/health' -m '1' || exit 1` verifies broker socket readiness immediately (<20ms) without waiting on `$SYS` intervals.

### 2.6 DevContainer Hybrid Portability (R2)
- **Observation**: `devcontainer.json` specifies `"service": "python-api"` and includes extensions for both Python and Rust (`rust-lang.rust-analyzer`, `ms-python.python`, etc.).
- **Deduction**: Attaching VS Code to `python-api` assumes `python-api` has the toolchains required by the extensions. If `python-api` is built only with Python dependencies, `rust-analyzer` will fail due to missing `cargo` and `rustc`.
- **Resolution**: Leverage DevContainer standard features in `devcontainer.json` (`ghcr.io/devcontainers/features/rust:1` and `ghcr.io/devcontainers/features/docker-outside-of-docker:1`), ensuring VS Code automatically installs Rust and Docker tools inside the development container without bloating production Dockerfiles.

### 2.7 R3 Limit Configuration Tool
- **Observation**: No setup tool or CLI menu currently exists in the repository.
- **Observation**: Host environment has Python 3.12, `yaml` (PyYAML), and `curses` available.
- **Observation**: Acceptance criteria requires: "O aplicativo/script de setup lê e modifica os limites de memória no `docker-compose.yml` de forma bem-sucedida, persistindo as mudanças."
- **Deduction**: A standalone Python CLI tool (`configure_limits.py`) is ideal. To satisfy both interactive human use and programmatic CI verification, it must provide:
  1. An interactive terminal menu (reading current limits, offering presets and custom inputs).
  2. Non-interactive CLI arguments (`--service <name> --limit <val>`, `--preset <name>`, `--show`, `--verify`).
  3. Safe persistence that updates `deploy.resources.limits.memory` while preserving file comments and formatting.

---

## 3. Caveats

1. **Docker Engine Execution on Host**: The survey was conducted inside WSL Ubuntu where the `docker` binary is not in `$PATH`. Container runtime tests (`docker-compose up`) could not be executed directly in this subshell turn. Verification relies on static specification analysis and official upstream image specifications.
2. **Postgres Tablespace Mount Lifecycle**: Docker `tmpfs` mounts are re-created empty whenever a container is stopped and recreated (`down` followed by `up`). If PostgreSQL creates tablespace files on `ram_data`, restarting the container without re-running initialization requires either an unlogged recreation hook or ephemeral live-state handling on startup.
3. **Existing Placeholder Files**: Directories `./postgres`, `./rust-core`, and `./python-api` are currently missing. `docker-compose build` or `docker-compose up` will fail immediately until these directories, their Dockerfiles, and `postgres/init.sql` are created.

---

## 4. Conclusion

The existing workspace configuration establishes a solid foundation for R1, R2, and R3, but contains three high-priority defects and several architectural gaps that will block milestone progression if unaddressed:

1. **PostgreSQL Alpine UID Mismatch**: `postgres:15-alpine` runs as UID 70, but `docker-compose.yml` mounts `tmpfs` with `uid=999`. This must be updated to `uid=70,gid=70,mode=0700,size=1G` to prevent `Permission denied` on tablespace creation.
2. **Missing Healthchecks on Application Containers**: `rust-core` and `python-api` lack `healthcheck` definitions, making it impossible to satisfy Acceptance Criterion #3 ("todos atingem o status healthy"). Healthcheck stanzas must be added for both services.
3. **Missing Service Dependency**: `python-api` must depend on `mosquitto: condition: service_healthy` to guarantee broker availability for command publishing.
4. **DevContainer Toolchain Alignment**: `devcontainer.json` should incorporate DevContainer Features (`rust` and `docker-outside-of-docker`) so that `rust-analyzer` and Docker tools work seamlessly inside the `python-api` container.
5. **R3 Setup Tool Architecture**: A dual-mode Python CLI application (`configure_limits.py`) should be implemented, providing an interactive menu for operators and CLI flags for automated acceptance testing, reading and persisting limits in `docker-compose.yml`.

---

## 5. Verification Method

### 5.1 Independent File and Syntax Verification
1. **Verify Docker Compose Configuration**:
   ```bash
   python3 -c "import yaml; doc = yaml.safe_load(open('docker-compose.yml')); print('Services:', list(doc['services'].keys())); print('Limits:', {s: doc['services'][s].get('deploy', {}).get('resources', {}).get('limits', {}) for s in doc['services']})"
   ```
   *Expected Output*: Displays all 4 services and their respective memory limits: `postgres: 1.5G`, `mosquitto: 500M`, `rust-core: 500M`, `python-api: 1G`.

2. **Verify Mosquitto Configuration**:
   ```bash
   cat mosquitto/mosquitto.conf
   ```
   *Expected Output*: Contains `listener 1883` and `allow_anonymous true`.

3. **Verify DevContainer Syntax**:
   ```bash
   python3 -c "import json; doc = json.load(open('.devcontainer/devcontainer.json')); print('Extensions:', doc['customizations']['vscode']['extensions'])"
   ```
   *Expected Output*: Contains `rust-lang.rust-analyzer`, `ms-python.python`, `ms-azuretools.vscode-docker`.

### 5.2 Verification of Proposed Fixes (Once Implemented)
1. **Postgres tmpfs ownership test**:
   Verify `docker-compose.yml` line 14 reflects `uid=70,gid=70` (or compatible ownership).
2. **Container health check test**:
   Verify all 4 services in `docker-compose.yml` have a `healthcheck` block with valid test commands.
3. **R3 Tool Verification**:
   Execute `./configure_limits.py --show` to verify parsing, and `./configure_limits.py --service postgres --limit 2G --dry-run` to verify modification logic without corrupting YAML formatting.

### 5.3 Invalidation Conditions
- If the PostgreSQL base image is switched from `postgres:15-alpine` to `postgres:15` (Debian-based), the UID changes from 70 to 999, invalidating the need for `uid=70`.
- If `rust-core` and `python-api` are configured without healthchecks, the acceptance test for all containers reaching `healthy` is guaranteed to fail.
