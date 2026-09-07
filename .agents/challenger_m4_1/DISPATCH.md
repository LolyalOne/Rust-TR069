## 2026-09-07T06:55:13Z
You are challenger_m4_1 (teamwork_preview_challenger).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/challenger_m4_1

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m4_api/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/main.py
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/routers/cpes.py
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/python-api/app/mqtt.py
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

TASK:
Adversarially challenge the Python FastAPI Manager:
1. Test error handling: 404 on non-existent CPE, 409 on duplicate serial_number, 503 on MQTT broker failure.
2. Verify cascading deletion: deleting a CPE in inventory cascades to live state and history.
3. Verify MQTT reboot payload format against `simulate_flow.sh` regex: does the payload match `reboot|operate`?
4. Run tests and/or execute adversarial test cases.
5. Deliver your explicit verdict (APPROVE or REQUEST_CHANGES) in `handoff.md` and message the orchestrator.
