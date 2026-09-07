# Task Assignment: Explorer 2 (XML/SOAP Inform Parser & Normalization)

## Context
Milestone 2 of Rust-TR069 Dual-Stack Refactoring: Implement XML/SOAP Parsing for TR-069 Inform packets.
Target component: `rust-core/src/cwmp.rs` (or module), XML parser library (`quick-xml` or `roxmltree`).

## Objectives
1. Investigate the best XML parsing library (`roxmltree` vs `quick-xml`) compatible with `rust-core`:
   - Need fast, zero-allocation or zero-copy parsing.
   - Resilient against vendor namespace prefixes (e.g. `SOAP-ENV`, `soapenv`, `cwmp`, default namespaces, `urn:dslforum-org:cwmp-1-0` through `cwmp-1-4`).
2. Analyze the exact parsing requirements for TR-069 `cwmp:Inform`:
   - Extract `DeviceId`: `Manufacturer`, `OUI`, `ProductClass`, `SerialNumber`.
   - Extract `Event` list (`EventCode`, `CommandKey`).
   - Extract `ParameterList`: all `<ParameterValueStruct>` with `<Name>` and `<Value>`.
   - Handle self-closing tags like `<Value xsi:type="xsd:string"/>`.
3. Analyze Optical Power normalization for both Huawei EchoLife and TP-Link:
   - Huawei TR-098: `InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower` or `InternetGatewayDevice.WANDevice.1.X_HW_OpticalInfo.RxPower`.
   - TP-Link TR-181: `Device.Optical.Interface.1.OpticalSignalLevel`.
   - Unit parsing: decimal string (`"-19.50"`), string with suffix (`"-19.50 dBm"`), integer scaling (`-1950` or `-19500`).
4. Design the data structures and parser functions:
   - Input: raw bytes / string slice of XML.
   - Output: `ParsedInform` struct containing device info, parameters map, optical power value, and session ID.
   - SOAP InformResponse generation: XML template with matching `<cwmp:ID>`.

## Artifacts to Read
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md` (MANDATORY)
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md`
- `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md`
- `rust-core/src/main.rs` (specifically `TelemetryUpdate` and `process_param`)

## Deliverables
Write your comprehensive analysis to `/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_2/handoff.md`.

## 2026-09-07T19:16:10Z
User Request:
You are Explorer 2 for Milestone 2 of the Rust-TR069 Dual-Stack Refactoring.
Your working directory is: /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_2

MANDATORY: You must read /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/ORIGINAL_REQUEST.md before starting work.
Also read:
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_2/DISPATCH.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/orchestrator_5/SCOPE.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/spec_miner_cwmp_1/handoff.md
- /mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/rust-core/src/main.rs

Investigate:
1. Fast XML parsing engine: evaluate `roxmltree` vs `quick-xml` for parsing SOAP/XML `<cwmp:Inform>` with tolerance for various namespace prefixes (SOAP-ENV, soapenv, cwmp-1-0 through cwmp-1-4).
2. Detail parsing logic for:
   - DeviceId: Manufacturer, OUI, ProductClass, SerialNumber.
   - Event list.
   - ParameterList (all ParameterValueStruct name/value pairs, handling self-closing Value tags).
3. Optical power extraction & normalization:
   - Huawei TR-098: X_HW_OpticalRxPower.
   - TP-Link TR-181: Device.Optical.Interface.1.OpticalSignalLevel.
   - Handling formats: decimal string ("-19.50"), with suffix ("-19.50 dBm"), scaled integers (-1950, -19500).
4. SOAP response generation for InformResponse echoing Header ID.

Write your report to:
/mnt/c/Users/Administrator/Documents/Projetos_Pessoais/Rust-TR069/.agents/explorer_m2_5_2/handoff.md
When done, message orchestrator with a brief summary referencing the handoff path.
