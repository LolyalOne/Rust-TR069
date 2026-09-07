## 2026-09-07T19:14:42Z
You are the Project Orchestrator for the Rust-TR069 Dual-Stack Refactoring (Milestone 2).

### Workspace & Directories
- Workspace Root: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069`
- Your Dedicated Working Directory: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5`
- Original Request: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md`

### Context & Status
The previous orchestrator and team completed Milestone 1 (Docker Compose port 7547, PostgreSQL `cpe_pending_commands` table, FastAPI models/schemas). Reference documentation exists in `.agents/orchestrator_4/PROJECT.md`, `.agents/orchestrator_4/GATE_STATUS.md`, and `.agents/spec_miner_cwmp_1/handoff.md`.

Your objective is to assume from **Milestone 2** and complete it:

### Requirements
1. **R1. Implement Embedded HTTP (CWMP) Server in Rust Core**:
   Modify `rust-core/src/main.rs` to run an embedded web server (e.g. `axum`) listening on port `7547` (TR-069 standard) alongside the existing Tokio MQTT subscriber.
2. **R2. XML/SOAP Parsing (TR-069 Inform)**:
   The Rust server must accept `POST` requests containing XML/SOAP packets (`<SOAP-ENV:Envelope>`, `<cwmp:Inform>`). Extract principal metadata (SerialNumber, Manufacturer, and TR-181/TR-098 parameters) using a fast XML library (`roxmltree` or `quick-xml`).
3. **R3. MPSC Convergence and Command Delivery**:
   Send extracted XML Inform data to the **same Tokio MPSC channel** that processes TR-369 MQTT messages for PostgreSQL ingestion. Query pending commands from PostgreSQL (`cpe_pending_commands`) written by Python API, and return them in the XML HTTP response (e.g. `GetParameterValues` or `InformResponse`).

### Acceptance Criteria
- [ ] `rust-core` compiles with zero errors with `axum` and the XML parser library.
- [ ] Port `7547` is active and accepting POST requests.
- [ ] All previous unit tests continue to pass without regression.

### Execution Instructions
- Maintain your `BRIEFING.md` and `progress.md` in `.agents/orchestrator_5/`.
- Decompose, dispatch specialists (workers, reviewers, challengers, auditors), enforce quality gates.
- When all requirements and acceptance criteria are satisfied, report completion with your victory claim to the Sentinel (parent) so that an independent Victory Audit can be conducted.
