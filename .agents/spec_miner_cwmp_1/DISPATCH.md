# Dispatch for spec_miner_cwmp_1

## Mission
Investigate and extract the exact technical specifications for TR-069 CWMP over HTTP/XML on port 7547, with special focus on consumer ONTs (specifically Huawei EchoLife and TP-Link EX).

## Inputs
- Mandatory: `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (specifically `## Follow-up — 2026-09-07T14:20:33Z`)
- TR-069 Amendment 5 / 6 CWMP protocol specifications, Huawei EchoLife Inform XML schema.

## Required Output
Write your findings to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md` including:
1. Exact structure of a TR-069 `cwmp:Inform` XML/SOAP payload as sent by Huawei EchoLife and TP-Link ONTs (`<SOAP-ENV:Envelope>`, `<cwmp:Inform>`, `<DeviceId>`, `<ParameterList>`, `<Event>`).
2. List of standard parameter names and TR-181/TR-098 paths for Serial Number, Manufacturer, ModelName, SoftwareVersion, ConnectionRequestURL, and Optical metrics (RxPower, TxPower, PON RX/TX).
3. The exact SOAP/XML response required for `cwmp:InformResponse` that the ACS must return to the ONT to acknowledge Inform (HTTP 200 OK + SOAP Envelope with `<cwmp:InformResponse>`).
4. The CWMP session lifecycle: how the ACS delivers pending RPC commands (`GetParameterValues`, `Reboot`) within the HTTP session after `InformResponse` (empty HTTP response or direct RPC request in SOAP Body).
5. Concrete sample payloads (both Huawei Inform request XML, InformResponse XML, and RPC command/response XML) suitable for integration testing with `curl`.

## 2026-09-07T14:24:30Z
You are spec_miner_cwmp_1.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1
Mandatory initial read:
1. Read /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md (specifically ## Follow-up — 2026-09-07T14:20:33Z).
2. Read your dispatch instructions at /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/DISPATCH.md.

Task:
Investigate and extract exact TR-069 CWMP specifications for consumer ONTs (specifically Huawei EchoLife and TP-Link EX):
- Detail the exact structure of a TR-069 cwmp:Inform XML/SOAP payload sent by Huawei EchoLife (SOAP Envelope, Body, Inform, DeviceId with Manufacturer and SerialNumber, ParameterList with TR-098 / TR-181 paths like Optical RX/TX power, connection URL, software version, hardware version).
- Detail the exact SOAP XML cwmp:InformResponse required from the ACS (HTTP 200 OK with SOAP envelope containing cwmp:InformResponse and MaxEnvelopes).
- Detail CWMP session management: how pending RPC methods (e.g. GetParameterValues, Reboot) are conveyed during the HTTP session following InformResponse.
- Provide concrete XML examples for Huawei Inform request, InformResponse, and RPC commands suitable for testing with curl.
- Write your comprehensive report to /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md.
- Send a completion message back when done referencing the handoff path.
