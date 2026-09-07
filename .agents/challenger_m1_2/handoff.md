# Milestone 1 Adversarial Challenge Report: Infrastructure & DevContainer

**Challenger**: `challenger_m1_2` (Archetype: `teamwork_preview_challenger`)  
**Role**: critic, specialist  
**Parent Conversation ID**: `6258e12c-9553-47a2-9624-69521a0b2d82`  
**Review Targets**:
- `/mnt/d/Projetos/TR069-181/docker-compose.yml`
- `/mnt/d/Projetos/TR069-181/.devcontainer/devcontainer.json`
**Interface Contracts & Scope**:
- `/mnt/d/Projetos/TR069-181/ORIGINAL_REQUEST.md` (R1, R2, Acceptance Criteria #1 and #3)
- `/mnt/d/Projetos/TR069-181/.agents/orchestrator_1/PROJECT.md` (Features 1–4, Milestone 1)
- `/mnt/d/Projetos/TR069-181/.agents/worker_m1_infra/handoff.md`

---

## Verdict: REQUEST_CHANGES

While the strict memory limits (1.5G, 500M, 500M, 1G) and JSON/YAML syntax strictly satisfy requirements, **DevContainer portability (R2 / Acceptance Criterion 1) has a critical functional deficiency**:
In `.devcontainer/devcontainer.json`, `"service": "python-api"` and `"workspaceFolder": "/workspace"` are specified, but `python-api` in `docker-compose.yml` defines **no volume mounts for `/workspace`**. Because the Dev Container Specification prohibits `"workspaceMount"` in `devcontainer.json` when using `dockerComposeFile` (empirically confirmed by schema validation), opening the project in VS Code Dev Containers loads an empty container folder disconnected from host files, preventing code editing and file persistence.

A 3-line modification to `docker-compose.yml` resolves this completely.

---

## 1. Observation

### 1.1 Memory Limits Verification (Adherence to R1)
Inspected `/mnt/d/Projetos/TR069-181/docker-compose.yml`:
- **Line 18 (`postgres`)**:
  ```yaml
  deploy:
    resources:
      limits:
        memory: 1.5G
  ```
- **Line 37 (`mosquitto`)**:
  ```yaml
  deploy:
    resources:
      limits:
        memory: 500M
  ```
- **Line 62 (`rust-core`)**:
  ```yaml
  deploy:
    resources:
      limits:
        memory: 500M
  ```
- **Line 89 (`python-api`)**:
  ```yaml
  deploy:
    resources:
      limits:
        memory: 1G
  ```
- **Line 14 (`postgres` tmpfs mount)**:
  ```yaml
  tmpfs:
    - /var/lib/postgresql/ram_data:uid=70,gid=70,mode=0700,size=1G
  ```
- **Empirical Confirmation**:
  Executed Compose Spec validation and PyYAML limit extraction script:
  ```
  postgres: memory limit = 1.5G (expected 1.5G) -> MATCH
  mosquitto: memory limit = 500M (expected 500M) -> MATCH
  rust-core: memory limit = 500M (expected 500M) -> MATCH
  python-api: memory limit = 1G (expected 1G) -> MATCH
  SUCCESS: docker-compose.yml is 100% VALID according to official Compose Spec schema!
  ```

### 1.2 Healthcheck Syntax and Feasibility on Minimal Images
Direct inspection of `healthcheck` blocks across all 4 services:
1. **`postgres` (lines 19-23)**:
   ```yaml
   healthcheck:
     test: ["CMD-SHELL", "pg_isready -U acs_user -d acs_db"]
     interval: 5s
     timeout: 5s
     retries: 5
   ```
   - Image: `postgres:15-alpine`.
   - Command feasibility: `pg_isready` is pre-packaged in the official Alpine postgres image.
   - Observation: No `start_period` is specified. Initial `initdb` and running `/docker-entrypoint-initdb.d/init.sql` must complete within `5 retries * 5s = 25s`.
2. **`mosquitto` (lines 38-44)**:
   ```yaml
   healthcheck:
     test: ["CMD-SHELL", "mosquitto_pub -h localhost -t 'probe/health' -m '1' || exit 1"]
     interval: 5s
     timeout: 3s
     retries: 5
     start_period: 5s
   ```
   - Image: `eclipse-mosquitto:2`.
   - Command feasibility: Official `eclipse-mosquitto:2` packages `mosquitto_pub`.
   - Broker config: `/mnt/d/Projetos/TR069-181/mosquitto/mosquitto.conf` has `listener 1883` and `allow_anonymous true`. Publishing anonymously to `probe/health` returns exit code 0 when listening.
3. **`rust-core` (lines 63-69)**:
   ```yaml
   healthcheck:
     test: ["CMD-SHELL", "test -f /tmp/healthy || exit 1"]
     interval: 5s
     timeout: 3s
     retries: 5
     start_period: 5s
   ```
   - Command feasibility: Relies on `CMD-SHELL` (which invokes `/bin/sh -c`).
   - Image constraint: If the production container is built `FROM scratch` or `FROM gcr.io/distroless/static`, `/bin/sh` does not exist, causing the healthcheck execution to fail immediately with OCI runtime error (`exec: "/bin/sh": stat /bin/sh: no such file or directory`).
   - Contract requirement: The image must include a POSIX shell (e.g. `alpine` or `debian-slim`), and the Rust worker binary must touch `/tmp/healthy` upon successful MQTT/DB connection.
4. **`python-api` (lines 90-96)**:
   ```yaml
   healthcheck:
     test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
     interval: 5s
     timeout: 3s
     retries: 5
     start_period: 5s
   ```
   - Command feasibility: `urllib.request` is standard library in Python, eliminating the need for `curl` or `wget`.
   - Verified empirically: If the endpoint returns non-200 or connection is refused, `urllib.request.urlopen` raises an uncaught exception and Python exits with code 1; if 200 OK, it exits with code 0.
   - Contract requirement: FastAPI in M4 must implement `GET /health` returning HTTP 200.

### 1.3 DevContainer JSON Schema and Features Validation
Inspected `/mnt/d/Projetos/TR069-181/.devcontainer/devcontainer.json`:
```json
{
  "name": "ACS Distribuido Dev Container",
  "dockerComposeFile": "../docker-compose.yml",
  "service": "python-api",
  "workspaceFolder": "/workspace",
  "features": {
    "ghcr.io/devcontainers/features/rust:1": {},
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": {}
  },
  "customizations": {
    "vscode": {
      "extensions": [
        "rust-lang.rust-analyzer",
        "ms-python.python",
        "ms-python.vscode-pylance",
        "ms-azuretools.vscode-docker",
        "tamasfe.even-better-toml"
      ],
      "settings": {
        "python.formatting.provider": "black",
        "editor.formatOnSave": true
      }
    }
  }
}
```
- **Schema Validation**:
  Validated against official specification schema `https://raw.githubusercontent.com/devcontainers/spec/main/schemas/devContainer.schema.json` via Python `jsonschema.Draft7Validator`:
  `SUCCESS: DevContainer JSON is 100% VALID according to official schema!`
- **Features Syntax**:
  - `ghcr.io/devcontainers/features/rust:1`: Valid official feature.
  - `ghcr.io/devcontainers/features/docker-outside-of-docker:1`: Valid official feature.
- **Extensions**:
  All 5 extensions are valid Marketplace IDs for Rust, Python, Docker, and TOML.

### 1.4 DevContainer Workspace Mount Defect
- Observation:
  In `docker-compose.yml`, service `python-api` (lines 72-98) defines NO `volumes:`.
- Specification Constraint:
  Attempting to add `"workspaceMount"` to `devcontainer.json` was empirically tested against the Dev Container Specification schema:
  `jsonschema.exceptions.ValidationError: Unevaluated properties are not allowed ('workspaceMount' was unexpected)`
  Per the Dev Container Specification (Section "Docker Compose"), workspace file mounting must be configured via Compose volumes.
- Runtime Result:
  When VS Code Dev Containers attaches to `service: "python-api"`, it looks for `workspaceFolder: "/workspace"`. Without a volume mount, `/workspace` is not bound to the host filesystem. Project files on the host are invisible inside the container, and edits made inside the container are not synced back to the host.

---

## 2. Logic Chain

1. **R1 Adherence**:
   - `ORIGINAL_REQUEST.md` (lines 19-20) requires: `Postgres (1.5 GB), Mosquitto (500 MB), Rust USP Core (500 MB) e Python FastAPI (1 GB)` and a `tmpfs` volume for PostgreSQL in-RAM data.
   - Observation 1.1 proves each service has the exact required string in `deploy.resources.limits.memory`.
   - The PostgreSQL `tmpfs` mount has `size=1G`, `uid=70,gid=70,mode=0700`. Because tmpfs allocations are backed by container memory, 1.0G leaves 500M of unreserved memory within the 1.5G cgroup limit for PostgreSQL processes, preventing OOM-kill crashes under peak load.

2. **Healthcheck Feasibility**:
   - Observation 1.2 demonstrates that `pg_isready` (Postgres), `mosquitto_pub` (Mosquitto), and `urllib.request` (Python) are native to their respective container environments.
   - For `rust-core`, the `CMD-SHELL` directive requires `/bin/sh`. This dictates that the M3 multi-stage Dockerfile must base its runtime stage on a distribution containing a shell (`alpine` or `debian-slim`) rather than `scratch` or `distroless`.
   - For `postgres`, initial bootstrap occurs without `start_period`. While 25 seconds is adequate for basic initialization, cold starts under slow I/O benefit from a `start_period: 10s` safeguard.

3. **DevContainer Portability Failure (R2 & Acceptance Criterion 1)**:
   - Acceptance Criterion 1 states: *"O DevContainer carrega com sucesso, provendo um ambiente funcional e padronizado."*
   - As proven in Observation 1.4, `python-api` has no volume mount connecting host `.` to `/workspace`.
   - Because the Dev Container spec prohibits `"workspaceMount"` when `dockerComposeFile` is active, the environment relies exclusively on `docker-compose.yml` to expose the workspace.
   - Without `volumes: - .:/workspace:cached` in `python-api`, the DevContainer opens an empty directory. The developer has no access to `rust-core`, `python-api`, or the project root.
   - We verified that adding `volumes: - .:/workspace:cached` to `docker-compose.yml` is safely parsed, modified, and preserved by `configure_limits.py` (tested via temporary simulation script without regression).

---

## 3. Adversarial Review Challenges

### Challenge Summary
**Overall Risk Assessment**: HIGH (DevContainer is non-functional for development without volume mount; healthcheck requires concrete runtime constraints in M3/M4).

### Challenge 1 (High): DevContainer Disconnected from Host Workspace
- **Assumption challenged**: `.devcontainer/devcontainer.json` specifies `"workspaceFolder": "/workspace"`, assuming the host project folder is mapped to `/workspace` inside `python-api`.
- **Attack scenario**: A developer opens the repository in VS Code Dev Containers. The containers build and start, but `/workspace` is an unmapped ephemeral directory. No host files are visible. Any edits made inside VS Code do not persist to host Git storage.
- **Blast radius**: Complete breakdown of DevContainer development experience (Acceptance Criterion 1 failure).
- **Mitigation**: Add the volume mount to `python-api` in `docker-compose.yml`:
  ```yaml
    volumes:
      - .:/workspace:cached
  ```

### Challenge 2 (Medium): Minimal Image Shell Incompatibility in `rust-core`
- **Assumption challenged**: `rust-core` healthcheck uses `CMD-SHELL test -f /tmp/healthy || exit 1`.
- **Attack scenario**: If Milestone 3 builds a minimal Rust production container using `FROM scratch` or `FROM gcr.io/distroless/static`, `/bin/sh` does not exist. The container fails to launch the healthcheck process (`exec: "/bin/sh": no such file or directory`), causing the container to become permanently unhealthy.
- **Blast radius**: `simulate_flow.sh` and dependent services fail pre-flight healthcheck gates.
- **Mitigation**: Document an explicit contract for M3: the runtime image for `rust-core` must be based on `alpine:3.19` or `debian:bookworm-slim`, and the Rust binary must execute `std::fs::File::create("/tmp/healthy")` once initialized.

### Challenge 3 (Low): PostgreSQL Cold-Boot Bootstrap Race
- **Assumption challenged**: `postgres` healthcheck has `retries: 5` with `interval: 5s` and no `start_period`.
- **Attack scenario**: On a slower host or during initial cluster formatting (`initdb` + schema creation), Postgres initialization may take >25s, causing healthcheck to report `unhealthy` prematurely and aborting `depends_on` dependents.
- **Blast radius**: False-negative failure on slower machines or cold Docker runs.
- **Mitigation**: Add `start_period: 10s` to `postgres.healthcheck`.

---

## 4. Stress Test Results

| # | Test Scenario | Expected Behavior | Actual Behavior | Result |
|---|---------------|-------------------|-----------------|:------:|
| 1 | Exact memory limit adherence in `docker-compose.yml` | 1.5G, 500M, 500M, 1G exact matches | Confirmed exact matches across all 4 services | **PASS** |
| 2 | Official Compose Specification schema validation | 100% schema compliance | Draft7Validator: 0 errors | **PASS** |
| 3 | Official DevContainer Specification schema validation | 100% schema compliance | Draft7Validator: 0 errors | **PASS** |
| 4 | DevContainer schema validation with `workspaceMount` | `workspaceMount` rejected with `dockerComposeFile` | Unevaluated property error caught by schema | **CONFIRMED** |
| 5 | Python API healthcheck probe execution | Raises exception on connection refused, exits 0 on 200 | Exit code 1 on refused connection; 0 on success | **PASS** |
| 6 | Preservation of `python-api` volumes by `configure_limits.py` | Line surgery preserves `volumes: - .:/workspace:cached` | Volume block preserved across limit mutations | **PASS** |
| 7 | Host workspace mapping in `python-api` | Host repo mapped to `/workspace` | No volume mapping exists in `docker-compose.yml` | **FAIL** |

---

## 5. Caveats

- **Host Docker Engine**: Direct invocation of `docker compose up` was not executed due to the absence of the Docker daemon in the current WSL environment. Verification was performed empirically via official JSON schemas, PyYAML AST/line inspection, Python network/exception simulations, and unit testing harnesses.
- **Milestone 3 & 4 Implementations**: The healthchecks for `rust-core` and `python-api` depend on code that will be created in M3 (`/tmp/healthy`) and M4 (`/health`). These are documented as interface contracts.

---

## 6. Conclusion & Actionable Recommendations

### Verdict: REQUEST_CHANGES

To clear this challenge and guarantee Acceptance Criterion 1, apply the following change:

**Target File**: `/mnt/d/Projetos/TR069-181/docker-compose.yml`  
Under `services.python-api`, add the workspace volume mount:

```yaml
  python-api:
    build:
      context: ./python-api
    volumes:
      - .:/workspace:cached
    environment:
      - DATABASE_URL=postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db
...
```

*(Optional best practice)*: Add `start_period: 10s` to `services.postgres.healthcheck`.

---

## 7. Verification Method

To independently reproduce the empirical findings:

1. **Verify memory limits and Compose Spec schema**:
   ```bash
   python3 -c "
   import yaml, urllib.request, json, jsonschema
   schema = json.loads(urllib.request.urlopen('https://raw.githubusercontent.com/compose-spec/compose-spec/master/schema/compose-spec.json').read().decode())
   doc = yaml.safe_load(open('docker-compose.yml'))
   jsonschema.validate(instance=doc, schema=schema)
   limits = {k: v.get('deploy', {}).get('resources', {}).get('limits', {}).get('memory') for k, v in doc['services'].items()}
   assert limits == {'postgres': '1.5G', 'mosquitto': '500M', 'rust-core': '500M', 'python-api': '1G'}
   print('Compose Schema & Limits: PASS')
   "
   ```

2. **Verify DevContainer schema compliance and `workspaceMount` restriction**:
   ```bash
   python3 -c "
   import urllib.request, json, jsonschema
   base_url = 'https://raw.githubusercontent.com/devcontainers/spec/main/schemas/'
   schema = json.loads(urllib.request.urlopen(base_url + 'devContainer.schema.json').read().decode())
   store = {'vscode://schemas/settings/machine': {'type': 'object'}, 'vscode://schemas/settings/resource': {'type': 'object'}, 'vscode://schemas/launch': {'type': 'object'}, 'vscode://schemas/tasks': {'type': 'object'}}
   resolver = jsonschema.RefResolver(base_uri=base_url, referrer=schema, store=store)
   config = json.load(open('.devcontainer/devcontainer.json'))
   jsonschema.Draft7Validator(schema, resolver=resolver).validate(config)
   print('DevContainer Schema: PASS')
   "
   ```

3. **Verify volume absence in `python-api`**:
   ```bash
   python3 -c "
   import yaml
   doc = yaml.safe_load(open('docker-compose.yml'))
   vols = doc['services']['python-api'].get('volumes', [])
   if not any('/workspace' in v for v in vols):
       print('FINDING REPRODUCED: python-api lacks /workspace volume mount')
   "
   ```

### Invalidation Conditions
- If `volumes: - .:/workspace:cached` is added to `python-api` in `docker-compose.yml`, this objection is resolved.
- If memory limits deviate from 1.5G, 500M, 500M, 1G.
