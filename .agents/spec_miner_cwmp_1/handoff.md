# TR-069 CWMP Specification & Integration Report: Consumer ONTs (Huawei EchoLife & TP-Link EX)

## 1. Observation

Direct observations from project requirements, existing codebase, and authoritative technical specifications:

1. **User Requirements (`ORIGINAL_REQUEST.md` — Section `Follow-up — 2026-09-07T14:20:33Z`)**:
   - **Dual-Stack Support**: The system must become dual-stack, simultaneously handling modern TR-369 / USP (Protobuf over MQTT on port 1883) and legacy consumer ONTs (Huawei EchoLife and TP-Link EX) communicating via **TR-069 Classic (CWMP over HTTP/XML on port 7547)**.
   - **R1 (Embedded HTTP Server)**: Run an embedded web server in `rust-core/src/main.rs` listening on port `7547` concurrently with the Tokio MQTT event loop.
   - **R2 (XML/SOAP Parsing)**: Receive HTTP `POST` requests containing `<SOAP-ENV:Envelope>` with `<cwmp:Inform>`, extracting device identifiers (`SerialNumber`, `Manufacturer`, `OUI`, `ProductClass`) and TR-098 / TR-181 parameters (Optical RX/TX power, software/hardware version, connection URL).
   - **R3 (MPSC Convergence)**: Transmit extracted telemetry into the **same Tokio MPSC channel** (`tokio::sync::mpsc::Sender<TelemetryUpdate>`) that already feeds the PostgreSQL database sink.
   - **R4 (Pending RPC Command Queuing)**: TR-069 uses a client-driven polling session model. Pending RPC commands (`GetParameterValues`, `Reboot`) must be delivered to the ONT within the active CWMP session following `InformResponse`.
   - **R5 (Docker Exposure)**: Expose port `7547:7547` on `rust-core` in `docker-compose.yml`.

2. **PostgreSQL Database Schema (`postgres/init.sql`)**:
   - `cpe_inventory` (lines 26–39): Persistent inventory table keyed by `cpe_id VARCHAR(128)`, with fields `serial_number`, `manufacturer`, `model`, `oui`, `product_class`, `hardware_version`, `software_version`, `status`.
   - `cpe_live_state` (lines 50–60): In-memory unlogged table in `ram_tablespace` (`tmpfs`), keyed by `cpe_id`, storing `current_parameters JSONB`, `telemetry_metrics JSONB`, `status`, `ip_address`, `firmware_version`, `last_seen`.
   - `reconcile_live_to_history()` trigger (lines 124–222): Automatically evaluates optical power variations:
     - Extracts `rx_optical_power` from `NEW.telemetry_metrics->>'rx_optical_power'` or `NEW.current_parameters->>'Device.Optical.Interface.1.OpticalSignalLevel'` / `'Device.Optical.Interface.1.RxPower'`.
     - Strictly inserts a historical audit record into `cpe_historical_metrics` if the optical signal variation exceeds 1.0 dBm (`|NEW - OLD| > 1.0 dBm`) or upon baseline initial reading (`initial_state`).

3. **Rust Core Architecture (`rust-core/src/main.rs`)**:
   - Asynchronous decoupled architecture using `tokio::sync::mpsc::channel::<TelemetryUpdate>(1024)`.
   - The struct `TelemetryUpdate` (lines 26–35) encapsulates:
     ```rust
     pub struct TelemetryUpdate {
         pub cpe_id: String,
         pub endpoint_id: Option<String>,
         pub status: String,
         pub current_parameters: JsonValue,
         pub telemetry_metrics: JsonValue,
         pub ip_address: Option<String>,
         pub firmware_version: Option<String>,
         pub timestamp: DateTime<Utc>,
     }
     ```
   - `run_db_sink` (lines 506–563) processes updates from the receiver, auto-provisions `cpe_inventory` to prevent foreign key errors (FK 23503), and performs an atomic UPSERT with JSONB concatenation into `cpe_live_state`.
   - `process_param` (lines 334–376) maps optical keys ending with `OpticalSignalLevel` or `RxPower` directly into `telemetry_metrics["rx_optical_power"]`.

4. **Authoritative CWMP Standards & Vendor Specifics**:
   - **Broadband Forum TR-069** (Amendments 1 through 6): CPE WAN Management Protocol (CWMP).
   - **Broadband Forum TR-098**: Internet Gateway Device (IGD) Data Model (`InternetGatewayDevice.`).
   - **Broadband Forum TR-181 Issue 2**: Device Data Model (`Device.`).
   - **Huawei EchoLife GPON ONTs** (HG8245H, HG8245Q, EG8145V5, HG8010H): Communicate using TR-069 over HTTP POST, predominantly utilizing TR-098 schema with vendor-specific extensions (`X_HW_OpticalRxPower`, `X_HW_OpticalTxPower`, `X_HW_OpticalInfo`).
   - **TP-Link EX Series ONTs/Routers** (EX220, EX511, Archer C5v, XC220-G3v): Support either TR-098 or TR-181 data models with standard `Device.Optical.Interface.1.OpticalSignalLevel` or `InternetGatewayDevice.WANDevice.1.WANEponInterfaceConfig.RxPower`.

---

## 2. Logic Chain

1. **Ingest Point Unification**:
   - The system needs to ingest data from both MQTT (TR-369 USP) and HTTP/SOAP (TR-069 CWMP).
   - By running an Axum HTTP listener on port `7547` inside `rust-core`, the same process holds the cloning end of `tokio::sync::mpsc::Sender<TelemetryUpdate>`.
   - An incoming TR-069 `cwmp:Inform` HTTP POST is received by the Axum route, parsed into device metadata and key-value parameters, converted to a `TelemetryUpdate`, and submitted to `tx.send(update).await`.
   - Zero modifications are needed to the downstream database sink or PostgreSQL reconciliation trigger.

2. **CPE Identifier Resolution**:
   - TR-069 `DeviceId` provides `Manufacturer`, `OUI`, `ProductClass`, and `SerialNumber`.
   - For consumer ONTs, `SerialNumber` is the universally unique physical identifier (e.g. `485754431234ABCD` for Huawei, where `48575443` is ASCII for `HWTC`).
   - Mapping `cpe_id = SerialNumber` provides 100% interoperability with `cpe_inventory.serial_number` and foreign key integrity.

3. **Telemetry & Optical Power Normalization**:
   - Huawei EchoLife ONTs report optical power in vendor paths:
     - `InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower`
     - `InternetGatewayDevice.WANDevice.1.X_HW_OpticalInfo.RxPower`
   - TP-Link ONTs report:
     - `Device.Optical.Interface.1.OpticalSignalLevel` or `InternetGatewayDevice.WANDevice.1.WANGponInterfaceConfig.RxPower`
   - In both cases, the XML parser extracts all parameters into `current_parameters`.
   - Any parameter key matching `*Optical*RxPower*` or `*OpticalSignalLevel*` is parsed as a float (e.g. `-19.50`) and stored into `telemetry_metrics["rx_optical_power"]`.
   - This directly engages the `reconcile_live_to_history()` trigger in PostgreSQL whenever the optical signal delta exceeds 1.0 dBm.

4. **Half-Duplex CWMP Session Model**:
   - In TR-069, the CPE is strictly the HTTP client; the ACS is the HTTP server.
   - The session consists of two serial phases:
     - **Phase 1 (CPE Phase)**: CPE posts `cwmp:Inform`. ACS acknowledges with `cwmp:InformResponse` (HTTP 200 OK containing `<cwmp:InformResponse><MaxEnvelopes>1</MaxEnvelopes></cwmp:InformResponse>`).
     - **Phase 2 (ACS Phase)**: CPE posts an **Empty HTTP POST** (`Content-Length: 0`). This indicates the CPE has no more requests and shifts session control to the ACS.
     - The ACS checks if any pending RPC commands exist for this CPE:
       - If a command is pending (e.g., `Reboot` or `GetParameterValues`), the ACS returns it in the HTTP 200 OK body.
       - The CPE executes the command and posts back `cwmp:RebootResponse` or `cwmp:GetParameterValuesResponse`.
       - When the ACS has no further commands to execute, it responds with an **Empty HTTP 200 OK** (`Content-Length: 0`).
       - The CPE receives the empty response, tears down the HTTP connection, and the session terminates cleanly.

---

## 3. Features Discovered

| # | Category | Feature | Description | Inputs | Outputs | Error Behavior | Discovered Via |
|---|----------|---------|-------------|--------|---------|----------------|----------------|
| 1 | CWMP Transport | HTTP Server Listener | Embedded HTTP listener binding to `0.0.0.0:7547` for CWMP traffic | HTTP POST to `/`, `/cwmp`, or `/tr069` | HTTP Response (SOAP XML or 200 OK Empty) | Returns 405 for GET/PUT, 500 on internal parsing failure | TR-069 Section 3.1 & ORIGINAL_REQUEST R1 |
| 2 | CWMP SOAP | SOAP Envelope Decoding | Parses standard SOAP 1.1 Envelope and Header | Raw XML bytes | Root `<SOAP-ENV:Envelope>` with Header and Body | Returns SOAP Fault (9002 / Client Error) if XML is malformed | TR-069 Section 3.3 |
| 3 | CWMP Inform | `cwmp:Inform` Method Parsing | Extracts mandatory Inform payload sent on CPE connection | XML `<cwmp:Inform>` or `<Inform>` | DeviceId, Event Array, ParameterList | Ignores unexpected elements; fails if DeviceId missing | TR-069 Section A.3.1.1 |
| 4 | CWMP Inform | DeviceId Identification | Extracts ONT identity: Manufacturer, OUI, ProductClass, SerialNumber | `<DeviceId>` child elements | Normalized `cpe_id` (SerialNumber) & metadata | Uses fallback "UNKNOWN" if missing | TR-069 Section 3.4.1 & Huawei HG8245H Inform |
| 5 | CWMP Inform | Event Codes Handling | Identifies reason for session initiation (`0 BOOTSTRAP`, `1 BOOT`, `2 PERIODIC`, `4 VALUE CHANGE`, `6 CONNECTION REQUEST`) | `<Event>` Array with `<EventStruct>` | Parsed list of event strings | Ignores unknown vendor event codes | TR-069 Section 3.7.1.5 |
| 6 | CWMP Inform | ParameterList Extraction | Extracts flat key-value pairs of all data model parameters | `<ParameterValueStruct>` elements with `<Name>` and `<Value>` | JSON Object of all parameters | Preserves string representation; handles self-closing `<Value/>` | TR-069 Section 3.3.3 |
| 7 | Data Model | TR-098 Optical Telemetry | Vendor GPON optical power on Huawei EchoLife ONTs | `InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower` | Floated dBm value mapped to `rx_optical_power` | Handles dBm suffix and decimal strings (e.g. `"-19.50"`) | Huawei HG8245H / EG8145V5 Firmware Spec |
| 8 | Data Model | TR-181 Optical Telemetry | Standard optical power on TP-Link EX / modern ONTs | `Device.Optical.Interface.1.OpticalSignalLevel` | Floated dBm value mapped to `rx_optical_power` | Supports 0.001 dBm integer (e.g. -19500) or direct decimal | TR-181 Issue 2 & TP-Link EX220 Spec |
| 9 | CWMP Response | `cwmp:InformResponse` Generation | Mandatory ACS acknowledgment for Inform method | Parsed Inform ID | HTTP 200 OK + `<cwmp:InformResponse><MaxEnvelopes>1</MaxEnvelopes></cwmp:InformResponse>` | If ID present in Header, echoes in response Header | TR-069 Section A.3.1.2 |
| 10 | Session Lifecycle | Empty HTTP POST Detection | CPE indicates completion of its requests via `POST` with `Content-Length: 0` | Empty HTTP POST body | Triggers ACS Request Phase | Treats body length == 0 as shift to ACS phase | TR-069 Section 3.7.1 |
| 11 | Session Lifecycle | Empty HTTP 200 OK Termination | ACS signals end of CWMP session when no pending commands remain | Empty command queue | HTTP 200 OK with `Content-Length: 0` | CPE closes connection | TR-069 Section 3.7.1 |
| 12 | RPC Dispatch | `cwmp:Reboot` RPC Delivery | Sends remote reboot command to ONT during ACS Request Phase | Command request (CommandKey) | SOAP XML `<cwmp:Reboot><CommandKey>...</CommandKey></cwmp:Reboot>` | ONT responds with `cwmp:RebootResponse` or `SOAP-ENV:Fault` | TR-069 Section A.3.2.7 |
| 13 | RPC Dispatch | `cwmp:GetParameterValues` RPC Delivery | Reads real-time parameter paths from ONT | Array of parameter path strings | SOAP XML `<cwmp:GetParameterValues>` | ONT returns values or fault 9005 | TR-069 Section A.3.2.2 |
| 14 | RPC Response | `cwmp:RebootResponse` Ingestion | ONT confirmation of reboot command reception | `<cwmp:RebootResponse/>` | Marks pending command as executed | Logs fault if ONT returns Fault | TR-069 Section A.3.2.7 |
| 15 | Error Handling | `SOAP-ENV:Fault` Processing | CPE error responses during RPC execution | `<SOAP-ENV:Fault>` with `<faultcode>` and `<cwmp:Fault>` | Structured error code and string | Maps fault code to command failure status in DB | TR-069 Section A.3.1.4 & Section 3.3.4 |
| 16 | MPSC Convergence | Homogenized Telemetry Ingestion | Dispatches parsed CWMP data directly into Tokio MPSC channel | `TelemetryUpdate` | Channel transfer to Postgres Sink | Drops on channel saturation (>500ms) with warning | ORIGINAL_REQUEST R3 |

---

## 4. Edge Cases

| # | Feature | Input / Condition | Observed / Required Behavior |
|---|---------|-------------------|------------------------------|
| 1 | Namespace Version Variance | ONT uses `urn:dslforum-org:cwmp-1-0`, `cwmp-1-1`, `cwmp-1-2`, or `cwmp-1-4` | XML parser MUST match tag names regardless of namespace URN version or strip the namespace prefix. |
| 2 | Self-Closing `<Value/>` Tag | Parameter in `<ParameterList>` has no content: `<Value xsi:type="xsd:string"/>` | Parser MUST extract an empty string `""` without panicking or failing the entire Inform parse. |
| 3 | Optical Power Format (String with Unit) | Parameter contains `"-19.45 dBm"` or `"-21.50"` | Parser must strip `" dBm"` or whitespace before converting to float `-19.45`. |
| 4 | Optical Power Format (Integer Scaling) | Parameter contains `-1945` (0.01 dBm) or `-19450` (0.001 dBm TR-181) | If value is an integer outside normal dBm range (`< -100` or `> 100`), divide by 100 or 1000 to normalize to standard dBm. |
| 5 | Serial Number Hex vs ASCII Prefix | Huawei serial number sent as `485754431234ABCD` (Hex for `HWTC...`) | Keep raw string as `serial_number` and `cpe_id` so it matches physical barcodes and GPON SN formats. |
| 6 | CPE Request Phase Empty Body | ONT sends `POST / HTTP/1.1` with `Content-Length: 0` | Server must NOT fail with 400 Bad Request; it must recognize this as TR-069 Empty POST and reply with pending RPC or Empty 200 OK. |
| 7 | Missing SOAP Header | ONT omits `<SOAP-ENV:Header>` entirely | Server must parse Body successfully without requiring Header. |
| 8 | Multiple Events in Inform | ONT sends `0 BOOTSTRAP` and `1 BOOT` and `4 VALUE CHANGE` in same `<Event>` array | Server extracts all events into list without truncating to the first entry. |
| 9 | Premature TCP Disconnect | ONT abruptly closes TCP connection before receiving `InformResponse` | Server MPSC pipeline must safely handle dropped connection without thread panic or resource leak. |
| 10 | Unregistered Device Inform | CPE connects that does not exist in `cpe_inventory` | Existing Postgres Sink auto-provisions `cpe_inventory` row (`AUTO_PROVISION_INVENTORY_SQL`), avoiding FK 23503 errors. |

---

## 5. Detailed Technical Specifications & Concrete Payloads

### 5.1 Structure of `cwmp:Inform` XML/SOAP Payload (Huawei EchoLife)

A Huawei EchoLife ONT (e.g. HG8245H / EG8145V5) sends an HTTP POST with the following exact XML payload upon boot or periodic interval:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">10001</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd.</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>485754431234ABCD</SerialNumber>
      </DeviceId>
      <Event soap-enc:arrayType="cwmp:EventStruct[2]">
        <EventStruct>
          <EventCode>2 PERIODIC</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
        <EventStruct>
          <EventCode>4 VALUE CHANGE</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
      <MaxEnvelopes>1</MaxEnvelopes>
      <CurrentTime>2026-09-07T14:30:00Z</CurrentTime>
      <RetryCount>0</RetryCount>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[13]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.Manufacturer</Name>
          <Value xsi:type="xsd:string">Huawei Technologies Co., Ltd.</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.ManufacturerOUI</Name>
          <Value xsi:type="xsd:string">00259E</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.ModelName</Name>
          <Value xsi:type="xsd:string">EchoLife HG8245H</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.Description</Name>
          <Value xsi:type="xsd:string">EchoLife HG8245H GPON Terminal</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.ProductClass</Name>
          <Value xsi:type="xsd:string">EchoLife HG8245H</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SerialNumber</Name>
          <Value xsi:type="xsd:string">485754431234ABCD</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.HardwareVersion</Name>
          <Value xsi:type="xsd:string">10A8.A</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">V5R019C00S105</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.ProvisioningCode</Name>
          <Value xsi:type="xsd:string">PROV_ISP_HUAWEI</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.ManagementServer.ConnectionRequestURL</Name>
          <Value xsi:type="xsd:string">http://192.168.100.1:7547/tr069</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value xsi:type="xsd:string">-19.50</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalTxPower</Name>
          <Value xsi:type="xsd:string">2.40</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.IP.Interface.1.IPv4Address.1.IPAddress</Name>
          <Value xsi:type="xsd:string">192.168.100.1</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

---

### 5.2 Structure of `cwmp:Inform` XML/SOAP Payload (TP-Link EX Series / TR-181)

A TP-Link EX series ONT/router (e.g. EX220) using TR-181 sends:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-2">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">20002</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>TP-Link</Manufacturer>
        <OUI>EC172F</OUI>
        <ProductClass>EX220</ProductClass>
        <SerialNumber>22081B12345678</SerialNumber>
      </DeviceId>
      <Event soap-enc:arrayType="cwmp:EventStruct[1]">
        <EventStruct>
          <EventCode>2 PERIODIC</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
      <MaxEnvelopes>1</MaxEnvelopes>
      <CurrentTime>2026-09-07T14:30:00Z</CurrentTime>
      <RetryCount>0</RetryCount>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[8]">
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.Manufacturer</Name>
          <Value xsi:type="xsd:string">TP-Link</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.ModelName</Name>
          <Value xsi:type="xsd:string">EX220</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.SerialNumber</Name>
          <Value xsi:type="xsd:string">22081B12345678</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">1.0.0 Build 20230510</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.HardwareVersion</Name>
          <Value xsi:type="xsd:string">v1.0</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.ManagementServer.ConnectionRequestURL</Name>
          <Value xsi:type="xsd:string">http://192.168.1.1:7547/tr069</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.Optical.Interface.1.OpticalSignalLevel</Name>
          <Value xsi:type="xsd:string">-21.30</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.Optical.Interface.1.TxPower</Name>
          <Value xsi:type="xsd:string">2.15</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

---

### 5.3 Structure of `cwmp:InformResponse` (ACS Acknowledgment)

The ACS **MUST** respond to `cwmp:Inform` with HTTP 200 OK and the following SOAP response:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">10001</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:InformResponse>
      <MaxEnvelopes>1</MaxEnvelopes>
    </cwmp:InformResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

**HTTP Headers returned by ACS**:
```http
HTTP/1.1 200 OK
Content-Type: text/xml; charset=utf-8
Content-Length: 489
Server: Rust-TR069-ACS/1.0
Set-Cookie: session=485754431234ABCD-session; Path=/
```

---

### 5.4 Structure of RPC Commands (ACS -> ONT)

When the ONT posts an **Empty HTTP POST** (`Content-Length: 0`) after receiving `InformResponse`, the ACS delivers any pending RPC commands.

#### A. Reboot Command (`cwmp:Reboot`)
```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">cmd_reboot_9988</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Reboot>
      <CommandKey>cmd-reboot-9988</CommandKey>
    </cwmp:Reboot>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

#### B. Reboot Response from ONT (`cwmp:RebootResponse`)
The ONT executes or schedules the reboot and responds via HTTP POST:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">cmd_reboot_9988</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:RebootResponse/>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

#### C. GetParameterValues Command (`cwmp:GetParameterValues`)
```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">cmd_gpv_123</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:GetParameterValues>
      <ParameterNames soap-enc:arrayType="xsd:string[2]">
        <string>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</string>
        <string>InternetGatewayDevice.DeviceInfo.SoftwareVersion</string>
      </ParameterNames>
    </cwmp:GetParameterValues>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

#### D. GetParameterValues Response from ONT (`cwmp:GetParameterValuesResponse`)
```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">cmd_gpv_123</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:GetParameterValuesResponse>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[2]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value xsi:type="xsd:string">-19.50</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">V5R019C00S105</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:GetParameterValuesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

#### E. SOAP Fault Response (`SOAP-ENV:Fault`)
If a command is rejected or invalid, the ONT returns:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">cmd_gpv_123</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <SOAP-ENV:Fault>
      <faultcode>Client</faultcode>
      <faultstring>CWMP fault</faultstring>
      <detail>
        <cwmp:Fault>
          <FaultCode>9005</FaultCode>
          <FaultString>Invalid parameter name</FaultString>
          <SetParameterValuesFault>
            <ParameterName>InternetGatewayDevice.InvalidPath</ParameterName>
            <FaultCode>9005</FaultCode>
            <FaultString>Invalid parameter name</FaultString>
          </SetParameterValuesFault>
        </cwmp:Fault>
      </detail>
    </SOAP-ENV:Fault>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

---

### 5.5 Complete CWMP Session Lifecycle Diagram

```
CPE (Huawei/TP-Link)                                   ACS (Rust Core 7547)
        |                                                        |
        |--- 1. POST / (cwmp:Inform) --------------------------->|
        |    Header: Content-Type: text/xml                      |
        |    Body: <SOAP-ENV:Envelope>...<cwmp:Inform>...        |
        |                                                        | [Parse Inform]
        |                                                        | [Send TelemetryUpdate to MPSC]
        |                                                        | [DB Sink writes cpe_live_state]
        |                                                        | [Trigger reconciles history if |Δ| > 1.0 dBm]
        |                                                        |
        |<-- 2. HTTP 200 OK (cwmp:InformResponse) ---------------|
        |    Header: Set-Cookie: session=48575443...             |
        |    Body: <cwmp:InformResponse><MaxEnvelopes>1...       |
        |                                                        |
        |--- 3. POST / (Empty Body, Content-Length: 0) --------->|
        |    Header: Cookie: session=48575443...                 |
        |    (CPE signals: No more requests from CPE)            |
        |                                                        | [Check pending commands for CPE]
        |                                                        | [Command found: Reboot]
        |                                                        |
        |<-- 4. HTTP 200 OK (cwmp:Reboot) -----------------------|
        |    Body: <SOAP-ENV:Envelope>...<cwmp:Reboot>...        |
        |                                                        |
        | [CPE executes reboot sequence]                         |
        |--- 5. POST / (cwmp:RebootResponse) ------------------->|
        |    Body: <cwmp:RebootResponse/>                        |
        |                                                        | [Mark command executed]
        |                                                        | [No more pending commands]
        |                                                        |
        |<-- 6. HTTP 200 OK (Empty Body, Content-Length: 0) -----|
        |    (ACS signals: Session Complete)                     |
        |                                                        |
        |=== [TCP Connection Closed / Session Terminated] =======|
```

---

## 6. Caveats

1. **Authentication**: TR-069 specifies optional HTTP Basic or Digest Authentication (RFC 2617). Most ISP provisioning environments disable authentication on local management VLANs or use fixed pre-shared credentials (e.g. `cpe_user`/`cpe_pass`). For development and testing, allowing unauthenticated requests is standard.
2. **Reverse Connection Request**: In TR-069, when an operator dispatches an immediate command from the GUI, the ACS makes an HTTP GET to the ONT's `ConnectionRequestURL` (e.g. `http://192.168.100.1:7547/tr069`), prompting the ONT to initiate a new Inform session (`6 CONNECTION REQUEST`). However, in real ISP deployments behind CGNAT or Carrier NAT, the ONT cannot be reached directly; the system relies on periodic Inform (polling) or long-polling HTTP connections.
3. **Optical Signal Power Scale Factors**:
   - Some firmware builds report raw optical power in units of 0.01 dBm (e.g. `-1950` meaning `-19.50 dBm`) or 0.001 dBm (e.g. `-19500`).
   - The parser normalization should detect:
     - If the number contains a decimal point (`.`): parse as float directly.
     - If the number is an integer `< -1000`: divide by 100 or 1000 to reach plausible optical range (-8 dBm to -35 dBm).
4. **Namespace Tolerance**: Huawei EchoLife ONTs frequently switch between `xmlns:cwmp="urn:dslforum-org:cwmp-1-0"` and `cwmp-1-2`. Robust XML tokenizers (like `quick-xml` or `roxmltree`) must match element local names (`Inform`, `DeviceId`, `ParameterList`, `Name`, `Value`) rather than hardcoded full namespace prefixes.

---

## 7. Conclusion

- TR-069 CWMP over HTTP port 7547 is fully compatible with the existing Rust-TR069 architecture.
- Ingesting TR-069 packets requires:
  1. An Axum HTTP server listener on `0.0.0.0:7547`.
  2. An XML parser (using `quick-xml` or `roxmltree`) that extracts `cpe_id`, `current_parameters`, and normalizes `rx_optical_power`.
  3. Dispatching `TelemetryUpdate` down the existing `tokio::sync::mpsc::channel`.
  4. Returning standard `cwmp:InformResponse` followed by handling the Empty POST handshake for pending RPC commands (`Reboot`, `GetParameterValues`).
- This design reuses 100% of the PostgreSQL tables (`cpe_inventory`, `cpe_live_state`, `cpe_historical_metrics`) and triggers without requiring any SQL schema migrations.

---

## 8. Verification Method

### Step 1: Verification with `curl` (Inform Request)
When `rust-core` is running and port `7547` is exposed:

```bash
# 1. Send Huawei EchoLife Inform XML payload
curl -s -X POST http://localhost:7547/ \
  -H "Content-Type: text/xml; charset=utf-8" \
  -H "SOAPAction: \"\"" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">10001</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei Technologies Co., Ltd.</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>EchoLife HG8245H</ProductClass>
        <SerialNumber>485754431234ABCD</SerialNumber>
      </DeviceId>
      <Event soap-enc:arrayType="cwmp:EventStruct[1]">
        <EventStruct>
          <EventCode>2 PERIODIC</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
      <MaxEnvelopes>1</MaxEnvelopes>
      <CurrentTime>2026-09-07T14:30:00Z</CurrentTime>
      <RetryCount>0</RetryCount>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[6]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.Manufacturer</Name>
          <Value xsi:type="xsd:string">Huawei Technologies Co., Ltd.</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.ModelName</Name>
          <Value xsi:type="xsd:string">EchoLife HG8245H</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SerialNumber</Name>
          <Value xsi:type="xsd:string">485754431234ABCD</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">V5R019C00S105</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.ManagementServer.ConnectionRequestURL</Name>
          <Value xsi:type="xsd:string">http://192.168.100.1:7547/tr069</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value xsi:type="xsd:string">-19.50</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>'
```

**Expected Result**:
- HTTP Response: `200 OK`
- Body contains `<cwmp:InformResponse><MaxEnvelopes>1</MaxEnvelopes></cwmp:InformResponse>`

### Step 2: Database Live State Verification
Verify that the device was auto-provisioned in `cpe_inventory` and updated in RAM `cpe_live_state`:

```bash
docker exec acs_postgres psql -U acs_user -d acs_db -c \
  "SELECT cpe_id, status, telemetry_metrics->>'rx_optical_power' AS rx_power, last_seen FROM cpe_live_state WHERE cpe_id = '485754431234ABCD';"
```

**Expected Output**:
```
     cpe_id      | status | rx_power |          last_seen            
------------------+--------+----------+-------------------------------
 485754431234ABCD | online | -19.5    | 2026-09-07 14:30:00...
```

### Step 3: Optical Reconciliation Trigger Verification
Verify that the initial baseline snapshot was written to `cpe_historical_metrics`:

```bash
docker exec acs_postgres psql -U acs_user -d acs_db -c \
  "SELECT cpe_id, optical_power, change_reason, recorded_at FROM cpe_historical_metrics WHERE cpe_id = '485754431234ABCD';"
```

**Expected Output**:
```
     cpe_id      | optical_power | change_reason |          recorded_at          
------------------+---------------+---------------+-------------------------------
 485754431234ABCD |        -19.50 | initial_state | 2026-09-07 14:30:00...
```
