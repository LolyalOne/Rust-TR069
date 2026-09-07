# Dead Ends Tracking

| Iteration | Approach Tried | Why It Failed | Files Touched |
|-----------|---------------|---------------|---------------|
| M2-It1 | Executing `CREATE TABLESPACE` inside `DO $$ ... $$` PL/pgSQL block | PostgreSQL explicitly raises `ERROR: CREATE TABLESPACE cannot be executed inside a transaction block`. | `postgres/init.sql` |
| M2-It1 | Unconditional `UPDATE cpe_inventory` inside live-state reconciliation trigger | Triggers synchronous WAL disk writes on every volatile telemetry write, defeating `UNLOGGED` RAM tablespace purpose. | `postgres/init.sql` |
| M2-It1 | `MockCpeDatabase` dictionary test asserting the presence of `DO $$` block | Masked the invalid SQL syntax and disconnected behavioral test from actual DDL. | `postgres/test_schema.py` |
