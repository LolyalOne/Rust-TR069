## 2026-09-07T06:39:32Z

<USER_REQUEST>
You are challenger_m3_1 (teamwork_preview_challenger).
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/challenger_m3_1

MANDATORY INPUT FILES TO READ FIRST:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/ORIGINAL_REQUEST.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_2/PROJECT.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/worker_m3_rust/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/src/main.rs
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/simulate_flow.sh

TASK:
Adversarially challenge the Rust USP Core implementation:
1. Test malformed payloads: invalid protobuf bytes, invalid JSON, empty bytes, non-UTF8 strings. Does `PayloadDecoder::decode` crash or handle gracefully?
2. Test topic filtering: does `is_command_topic` correctly filter `usp/endpoint/cpe1/request`, `usp/endpoint/cpe1/request/sub`, while allowing `usp/endpoint/cpe1/notify` and `usp/endpoint/cpe1/telemetry`?
3. Run cargo tests or write adversarial test cases.
4. Deliver your explicit verdict (APPROVE or REQUEST_CHANGES) in `handoff.md` and message the orchestrator.
</USER_REQUEST>
