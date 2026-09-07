# Dispatch for worker_m1_it3

## Mission: Fix Protocol Query Parameter Edge Case in `python-api`
Address the defect discovered by `challenger_m1_it2_1` in `python-api/app/routers/cpes.py`.

## Inputs
- Mandatory: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md`
- Scope: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_4/PROJECT.md`
- Challenger Report: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/challenger_m1_it2_1/handoff.md`

## Required Change
In `python-api/app/routers/cpes.py` around line 232 in `reboot_cpe`:
Replace:
```python
raw_proto = protocol or "dual"
```
With:
```python
raw_proto = "dual" if protocol is None else protocol
```
And verify:
When `?protocol=` is passed (empty string `""`), `proto` is `""`. Since `""` is not in `("tr069", "tr369", "dual")`, it will raise `HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid protocol ''. Allowed: 'tr069', 'tr369', 'dual'")`.

Also, in `update_cpe_command` (around line 403), if `update_data.get("status") is None` was sent explicitly in JSON body (e.g. `{"status": null}`), ensure it does not attempt to set `status = None` causing a database `IntegrityError (500)`. Instead raise `HTTPException(400, "Status cannot be null")`.

Run tests:
`PYTHONPATH=python-api pytest python-api/tests/ -v`

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write your report to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m1_it3/handoff.md`.
