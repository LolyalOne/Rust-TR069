//! TR-069 CWMP XML/SOAP Parsing and Normalization Engine
//!
//! Provides zero-allocation, robust XML parsing resilient to vendor namespace prefixes
//! (Huawei TR-098, TP-Link TR-181), optical power telemetry normalization to standard float dBm,
//! and standard SOAP envelope generation for InformResponse, Reboot, and GetParameterValues RPCs.

use anyhow::{anyhow, Result};
use chrono::Utc;
use roxmltree::Document;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value as JsonValue};

use crate::TelemetryUpdate;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CwmpEvent {
    pub event_code: String,
    pub command_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParsedInform {
    pub cpe_id: String,
    pub manufacturer: String,
    pub oui: String,
    pub product_class: String,
    pub serial_number: String,
    pub header_id: String,
    pub events: Vec<CwmpEvent>,
    pub parameters: Map<String, JsonValue>,
    pub rx_optical_power: Option<f64>,
    pub tx_optical_power: Option<f64>,
    pub ip_address: Option<String>,
    pub software_version: Option<String>,
    pub hardware_version: Option<String>,
}

impl ParsedInform {
    /// Convert parsed CWMP Inform into unified TelemetryUpdate for MPSC channel
    pub fn into_telemetry_update(self, client_ip: Option<String>) -> TelemetryUpdate {
        let mut metrics = Map::new();
        if let Some(rx) = self.rx_optical_power {
            metrics.insert("rx_optical_power".to_string(), json!(rx));
        }
        if let Some(tx) = self.tx_optical_power {
            metrics.insert("tx_optical_power".to_string(), json!(tx));
        }

        let ip_address = client_ip.or(self.ip_address);

        TelemetryUpdate {
            cpe_id: self.cpe_id.clone(),
            endpoint_id: Some(self.cpe_id),
            status: "online".to_string(),
            current_parameters: JsonValue::Object(self.parameters),
            telemetry_metrics: JsonValue::Object(metrics),
            ip_address,
            firmware_version: self.software_version,
            timestamp: Utc::now(),
        }
    }
}

/// Evaluates if parameter key represents optical Rx power
pub fn is_optical_rx_power_key(key: &str) -> bool {
    let key_lower = key.to_lowercase();
    if key_lower.contains("txpower")
        || key_lower.contains("tx_power")
        || key.ends_with("TxPower")
        || key.ends_with("TransmitOpticalLevel")
        || key.contains("X_HW_OpticalTxPower")
    {
        return false;
    }

    key.contains("X_HW_OpticalRxPower")
        || key.ends_with("OpticalSignalLevel")
        || key.ends_with("RxPower")
        || key_lower.contains("rx_optical_power")
        || key_lower.contains("rx_power")
        || key_lower.contains("rxpower")
        || (key_lower.contains("optical") && key_lower.contains("rx"))
        || ((key_lower.contains("gpon") || key_lower.contains("epon")) && key_lower.contains("rx"))
}

/// Evaluates if parameter key represents optical Tx power
pub fn is_optical_tx_power_key(key: &str) -> bool {
    let key_lower = key.to_lowercase();
    key.ends_with("TxPower")
        || key.ends_with("X_HW_OpticalTxPower")
        || key.ends_with("TransmitOpticalLevel")
        || key_lower.contains("tx_optical_power")
        || key_lower.contains("tx_power")
        || key_lower.contains("txpower")
        || (key_lower.contains("optical") && key_lower.contains("tx"))
        || ((key_lower.contains("gpon") || key_lower.contains("epon")) && key_lower.contains("tx"))
}

/// Evaluates if parameter key represents optical power (Rx or Tx)
pub fn is_optical_power_key(key: &str) -> bool {
    is_optical_rx_power_key(key) || is_optical_tx_power_key(key)
}

/// Normalizes optical power parameter value if key is an optical Rx power metric.
///
/// Converts Huawei TR-098 and TP-Link TR-181 formats (decimal dBm strings,
/// unit suffixes, 0.01 dBm scale, 0.001 dBm millidBm) to standard float dBm (e.g. -19.50).
pub fn normalize_optical_power(key: &str, val: &str) -> Option<f64> {
    if !key.is_empty() && !is_optical_rx_power_key(key) {
        return None;
    }
    parse_optical_power(val)
}

/// Normalizes various vendor optical power string and integer formats into standard dBm (f64).
///
/// Physical GPON/EPON dynamic range: -60 dBm to +30 dBm.
/// Returns None on empty, non-numeric, disconnected fiber (LOS, "0", "N/A"), or out-of-range values.
pub fn parse_optical_power(raw: &str) -> Option<f64> {
    let trimmed = raw.trim();
    if trimmed.is_empty()
        || trimmed.eq_ignore_ascii_case("n/a")
        || trimmed == "--"
        || trimmed == "0"
        || trimmed == "0.0"
        || trimmed == "0.00"
    {
        return None;
    }

    // 1. Strip unit suffixes (dBm, dbm, dB) and parenthetical annotations
    let lower = trimmed.to_lowercase();
    let text = if let Some(idx) = lower.find("dbm") {
        &trimmed[..idx]
    } else if let Some(idx) = lower.find("db") {
        &trimmed[..idx]
    } else if let Some(idx) = lower.find('(') {
        &trimmed[..idx]
    } else {
        trimmed
    };

    // 2. Filter valid numeric characters: digits, signs, decimal point
    let mut cleaned = String::with_capacity(text.len());
    for ch in text.trim().chars() {
        if ch.is_ascii_digit() || ch == '-' || ch == '+' || ch == '.' {
            cleaned.push(ch);
        }
    }

    if cleaned.is_empty() || cleaned == "-" || cleaned == "+" {
        return None;
    }

    // Guard against 0 values (e.g. "0 dBm", "00")
    if cleaned == "0"
        || cleaned == "-0"
        || cleaned == "+0"
        || cleaned == "0.0"
        || cleaned == "0.00"
    {
        return None;
    }

    // 3. Format Branch A: Decimal string (e.g. "-19.50", "-21.3")
    if cleaned.contains('.') {
        if let Ok(val) = cleaned.parse::<f64>() {
            if (-60.0..=30.0).contains(&val) && val != 0.0 {
                return Some((val * 100.0).round() / 100.0);
            }
        }
        return None;
    }

    // 4. Format Branch B: Integer representations (scaled units)
    if let Ok(ival) = cleaned.parse::<i64>() {
        if (-60..=-1).contains(&ival) {
            // Unscaled coarse negative integer dBm (e.g. -19 -> -19.0)
            return Some(ival as f64);
        } else if (-9999..=-61).contains(&ival) {
            // Huawei / ZTE 0.01 dBm scale (e.g. -1950 -> -19.50)
            return Some(((ival as f64) / 100.0 * 100.0).round() / 100.0);
        } else if (-99999..=-10000).contains(&ival) {
            // TR-181 Issue 2 millidBm 0.001 dBm scale (e.g. -19500 -> -19.50)
            return Some(((ival as f64) / 1000.0 * 100.0).round() / 100.0);
        } else if (1..=30).contains(&ival) {
            // Unscaled coarse positive integer dBm (e.g. 2 -> 2.0)
            return Some(ival as f64);
        } else if (31..=999).contains(&ival) {
            // Positive Tx power 0.01 dBm scale (e.g. 240 -> 2.40)
            return Some(((ival as f64) / 100.0 * 100.0).round() / 100.0);
        } else if (1000..=50000).contains(&ival) {
            // Positive Tx power 0.001 dBm scale (e.g. 2400 -> 2.40)
            return Some(((ival as f64) / 1000.0 * 100.0).round() / 100.0);
        }
    }

    None
}

/// Injects a single missing namespace prefix declaration into the root element tag
fn inject_single_namespace(xml: &str, prefix: &str, uri: &str) -> Option<String> {
    let mut search_idx = 0;
    while let Some(open_idx) = xml[search_idx..].find('<') {
        let abs_open = search_idx + open_idx;
        let rest = &xml[abs_open..];
        if rest.starts_with("<?") || rest.starts_with("<!--") || rest.starts_with("<!DOCTYPE") {
            let close_idx = rest.find('>')?;
            search_idx = abs_open + close_idx + 1;
            continue;
        }
        // Found the root element tag
        let tag_name_end = rest.find(|c: char| c.is_whitespace() || c == '>')?;
        let insert_pos = abs_open + tag_name_end;
        let ns_decl = format!(r#" xmlns:{}="{}""#, prefix, uri);
        let mut fixed = String::with_capacity(xml.len() + ns_decl.len());
        fixed.push_str(&xml[..insert_pos]);
        fixed.push_str(&ns_decl);
        fixed.push_str(&xml[insert_pos..]);
        return Some(fixed);
    }
    None
}

/// Parses a TR-069 SOAP Inform XML string
pub fn parse_inform(xml_str: &str) -> Result<ParsedInform> {
    let mut current_xml: Option<String> = None;

    // Handle vendor XML firmware variations with undeclared or case-mismatched namespace prefixes
    for _ in 0..5 {
        let xml_ref = current_xml.as_deref().unwrap_or(xml_str);
        match Document::parse(xml_ref) {
            Ok(_) => break,
            Err(e) => {
                let err_msg = e.to_string();
                if let Some(start) = err_msg.find("unknown namespace prefix '") {
                    let rest = &err_msg[start + 26..];
                    if let Some(end) = rest.find('\'') {
                        let prefix = &rest[..end];
                        let uri = match prefix.to_ascii_lowercase().as_str() {
                            "soap-enc" => "http://schemas.xmlsoap.org/soap/encoding/",
                            "soap-env" | "soapenv" | "soap" => {
                                "http://schemas.xmlsoap.org/soap/envelope/"
                            }
                            "cwmp" => "urn:dslforum-org:cwmp-1-0",
                            "xsi" => "http://www.w3.org/2001/XMLSchema-instance",
                            "xsd" => "http://www.w3.org/2001/XMLSchema",
                            _ => "urn:dslforum-org:cwmp:auto",
                        };
                        if let Some(fixed) = inject_single_namespace(xml_ref, prefix, uri) {
                            current_xml = Some(fixed);
                            continue;
                        }
                    }
                }
                break;
            }
        }
    }

    let xml_ref = current_xml.as_deref().unwrap_or(xml_str);
    let doc = Document::parse(xml_ref).map_err(|e| anyhow!("XML parse error: {e}"))?;

    // 1. Extract Header ID (cwmp:ID or ID)
    let header_id = doc
        .descendants()
        .find(|n| n.tag_name().name() == "ID")
        .and_then(|n| n.text())
        .unwrap_or("1")
        .trim()
        .to_string();

    // 2. Locate cwmp:Inform node inside Body
    let inform_node = doc
        .descendants()
        .find(|n| n.tag_name().name() == "Inform")
        .ok_or_else(|| anyhow!("Missing <cwmp:Inform> element in SOAP Body"))?;

    // 3. Extract DeviceId
    let dev_node = inform_node
        .descendants()
        .find(|n| n.tag_name().name() == "DeviceId")
        .ok_or_else(|| anyhow!("Missing <DeviceId> element in Inform"))?;

    let manufacturer = dev_node
        .children()
        .find(|n| n.tag_name().name() == "Manufacturer")
        .and_then(|n| n.text())
        .unwrap_or("")
        .trim()
        .to_string();

    let oui = dev_node
        .children()
        .find(|n| n.tag_name().name() == "OUI")
        .and_then(|n| n.text())
        .unwrap_or("")
        .trim()
        .to_string();

    let product_class = dev_node
        .children()
        .find(|n| n.tag_name().name() == "ProductClass")
        .and_then(|n| n.text())
        .unwrap_or("")
        .trim()
        .to_string();

    let serial_number = dev_node
        .children()
        .find(|n| n.tag_name().name() == "SerialNumber")
        .and_then(|n| n.text())
        .unwrap_or("")
        .trim()
        .to_string();

    let cpe_id = if !serial_number.is_empty() {
        serial_number.clone()
    } else {
        format!("{}-{}-{}", oui, product_class, serial_number)
    };

    // 4. Extract Events
    let mut events = Vec::new();
    if let Some(event_node) = inform_node.descendants().find(|n| n.tag_name().name() == "Event") {
        for ev in event_node.descendants().filter(|n| n.tag_name().name() == "EventStruct") {
            let code = ev
                .children()
                .find(|n| n.tag_name().name() == "EventCode")
                .and_then(|n| n.text())
                .unwrap_or("")
                .trim()
                .to_string();
            let key = ev
                .children()
                .find(|n| n.tag_name().name() == "CommandKey")
                .and_then(|n| n.text())
                .unwrap_or("")
                .trim()
                .to_string();
            events.push(CwmpEvent {
                event_code: code,
                command_key: key,
            });
        }
    }

    // 5. Extract ParameterList
    let mut parameters = Map::new();
    let mut rx_optical_power = None;
    let mut tx_optical_power = None;
    let mut ip_address = None;
    let mut software_version = None;
    let mut hardware_version = None;

    if let Some(param_list_node) = inform_node.descendants().find(|n| n.tag_name().name() == "ParameterList") {
        for pvs in param_list_node.descendants().filter(|n| n.tag_name().name() == "ParameterValueStruct") {
            let name = pvs
                .children()
                .find(|n| n.tag_name().name() == "Name")
                .and_then(|n| n.text())
                .unwrap_or("")
                .trim();

            let value = pvs
                .children()
                .find(|n| n.tag_name().name() == "Value")
                .and_then(|n| n.text())
                .unwrap_or("")
                .trim();

            if name.is_empty() {
                continue;
            }

            parameters.insert(name.to_string(), JsonValue::String(value.to_string()));

            // Optical Rx Power detection
            if let Some(pwr) = normalize_optical_power(name, value) {
                rx_optical_power = Some(pwr);
            }

            // Optical Tx Power detection
            if is_optical_tx_power_key(name) {
                if let Some(pwr) = parse_optical_power(value) {
                    tx_optical_power = Some(pwr);
                }
            }

            // Software & Hardware Versions
            if name.ends_with("SoftwareVersion") {
                software_version = Some(value.to_string());
            } else if name.ends_with("HardwareVersion") {
                hardware_version = Some(value.to_string());
            }

            // IP Address
            if name.ends_with("IPv4Address.1.IPAddress") || name.ends_with("ExternalIPAddress") {
                ip_address = Some(value.to_string());
            } else if name.ends_with("ConnectionRequestURL") && ip_address.is_none() {
                if let Some(rest) = value.strip_prefix("http://").or_else(|| value.strip_prefix("https://")) {
                    let host_port = rest.split('/').next().unwrap_or("");
                    let host = host_port.split(':').next().unwrap_or("");
                    if !host.is_empty() {
                        ip_address = Some(host.to_string());
                    }
                }
            }
        }
    }

    Ok(ParsedInform {
        cpe_id,
        manufacturer,
        oui,
        product_class,
        serial_number,
        header_id,
        events,
        parameters,
        rx_optical_power,
        tx_optical_power,
        ip_address,
        software_version,
        hardware_version,
    })
}

/// Generates a standard cwmp:InformResponse SOAP Envelope echoing the Header ID
pub fn generate_inform_response(id: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:InformResponse>
      <MaxEnvelopes>1</MaxEnvelopes>
    </cwmp:InformResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        id
    )
}

/// Alias for generate_inform_response
pub fn build_inform_response(id: &str) -> String {
    generate_inform_response(id)
}

/// Generates a standard cwmp:Reboot SOAP RPC Envelope
pub fn generate_reboot_rpc(id: &str, command_key: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Reboot>
      <CommandKey>{}</CommandKey>
    </cwmp:Reboot>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        id, command_key
    )
}

/// Alias for generate_reboot_rpc
pub fn build_reboot_rpc_xml(id: &str, command_key: &str) -> String {
    generate_reboot_rpc(id, command_key)
}

/// Generates a standard cwmp:GetParameterValues SOAP RPC Envelope
pub fn generate_get_parameter_values_rpc(id: &str, names: &[String]) -> String {
    let mut param_elements = String::new();
    for name in names {
        param_elements.push_str(&format!("        <string>{}</string>\n", name));
    }

    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:GetParameterValues>
      <ParameterNames soap-enc:arrayType="xsd:string[{}]">
{}      </ParameterNames>
    </cwmp:GetParameterValues>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        id,
        names.len(),
        param_elements
    )
}

/// Alias for generate_get_parameter_values_rpc
pub fn build_get_parameter_values_rpc_xml(id: &str, names: &[String]) -> String {
    generate_get_parameter_values_rpc(id, names)
}

/// Generates a SOAP Fault Envelope
pub fn build_soap_fault(fault_code: &str, fault_string: &str, header_id: Option<&str>) -> String {
    let header = match header_id {
        Some(id) => format!(
            r#"  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">{}</cwmp:ID>
  </SOAP-ENV:Header>"#,
            id
        ),
        None => String::new(),
    };

    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
{}
  <SOAP-ENV:Body>
    <SOAP-ENV:Fault>
      <faultcode>{}</faultcode>
      <faultstring>{}</faultstring>
    </SOAP-ENV:Fault>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        header, fault_code, fault_string
    )
}

#[cfg(test)]
pub mod tests {
    use super::*;

    #[test]
    fn test_optical_power_normalization_all_formats() {
        // 1. Decimal strings
        assert_eq!(parse_optical_power("-19.50"), Some(-19.50));
        assert_eq!(parse_optical_power("-21.3"), Some(-21.30));

        // 2. Decimal strings with dBm suffix and spacing
        assert_eq!(parse_optical_power("-19.50 dBm"), Some(-19.50));
        assert_eq!(parse_optical_power("-19.50dBm"), Some(-19.50));
        assert_eq!(parse_optical_power(" -19.50  dBm "), Some(-19.50));

        // 3. Huawei / ZTE scaled integers (0.01 dBm scale)
        assert_eq!(parse_optical_power("-1950"), Some(-19.50));
        assert_eq!(parse_optical_power("-2145"), Some(-21.45));

        // 4. TP-Link TR-181 scaled integers (0.001 dBm millidBm scale)
        assert_eq!(parse_optical_power("-19500"), Some(-19.50));
        assert_eq!(parse_optical_power("-21300"), Some(-21.30));

        // 5. Positive Tx optical power
        assert_eq!(parse_optical_power("2.40 dBm"), Some(2.40));
        assert_eq!(parse_optical_power("240"), Some(2.40));
        assert_eq!(parse_optical_power("2400"), Some(2.40));

        // 6. Non-numeric / fiber disconnected (LOS)
        assert_eq!(parse_optical_power(""), None);
        assert_eq!(parse_optical_power("N/A"), None);
        assert_eq!(parse_optical_power("--"), None);
        assert_eq!(parse_optical_power("0"), None);
        assert_eq!(parse_optical_power("0.0"), None);
        assert_eq!(parse_optical_power("0 dBm"), None);

        // 7. normalize_optical_power key checking
        assert_eq!(
            normalize_optical_power("InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower", "-19.50 dBm"),
            Some(-19.50)
        );
        assert_eq!(
            normalize_optical_power("Device.Optical.Interface.1.OpticalSignalLevel", "-21300"),
            Some(-21.30)
        );
        assert_eq!(
            normalize_optical_power("Device.DeviceInfo.SoftwareVersion", "1.0.0"),
            None
        );
    }

    #[test]
    fn test_parse_huawei_echolife_inform() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
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
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[4]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.HardwareVersion</Name>
          <Value xsi:type="xsd:string">10A8.A</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value xsi:type="xsd:string">V5R019C00S105</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value xsi:type="xsd:string">-19.50 dBm</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.ProvisioningCode</Name>
          <Value xsi:type="xsd:string"/>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

        let parsed = parse_inform(xml).expect("Huawei EchoLife Inform parsing must succeed");
        assert_eq!(parsed.header_id, "10001");
        assert_eq!(parsed.cpe_id, "485754431234ABCD");
        assert_eq!(parsed.manufacturer, "Huawei Technologies Co., Ltd.");
        assert_eq!(parsed.product_class, "EchoLife HG8245H");
        assert_eq!(parsed.events.len(), 2);
        assert_eq!(parsed.events[0].event_code, "2 PERIODIC");
        assert_eq!(parsed.rx_optical_power, Some(-19.50));
        assert_eq!(parsed.software_version, Some("V5R019C00S105".to_string()));

        // Verify self-closing Value handling
        let prov_code = parsed
            .parameters
            .get("InternetGatewayDevice.DeviceInfo.ProvisioningCode")
            .and_then(|v| v.as_str());
        assert_eq!(prov_code, Some(""));

        // Verify conversion to TelemetryUpdate
        let update = parsed.into_telemetry_update(Some("192.168.100.1".to_string()));
        assert_eq!(update.cpe_id, "485754431234ABCD");
        assert_eq!(update.status, "online");
        assert_eq!(
            update.telemetry_metrics.get("rx_optical_power").and_then(|v| v.as_f64()),
            Some(-19.50)
        );
        assert_eq!(update.ip_address, Some("192.168.100.1".to_string()));
    }

    #[test]
    fn test_parse_tplink_tr181_inform() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope 
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-2">
  <soapenv:Header>
    <cwmp:ID mustUnderstand="1">20002</cwmp:ID>
  </soapenv:Header>
  <soapenv:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>TP-Link</Manufacturer>
        <OUI>EC172F</OUI>
        <ProductClass>EX220</ProductClass>
        <SerialNumber>22081B12345678</SerialNumber>
      </DeviceId>
      <Event>
        <EventStruct>
          <EventCode>2 PERIODIC</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
      <ParameterList>
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.SoftwareVersion</Name>
          <Value>1.0.0 Build 20230510</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>Device.Optical.Interface.1.OpticalSignalLevel</Name>
          <Value>-21300</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </soapenv:Body>
</soapenv:Envelope>"#;

        let parsed = parse_inform(xml).expect("TP-Link TR-181 Inform parsing must succeed");
        assert_eq!(parsed.header_id, "20002");
        assert_eq!(parsed.cpe_id, "22081B12345678");
        assert_eq!(parsed.manufacturer, "TP-Link");
        assert_eq!(parsed.product_class, "EX220");
        assert_eq!(parsed.rx_optical_power, Some(-21.30));
    }

    #[test]
    fn test_build_inform_response_echoes_header_id() {
        let resp = generate_inform_response("REQ-9988-ABC");
        assert!(resp.contains("<cwmp:ID mustUnderstand=\"1\">REQ-9988-ABC</cwmp:ID>"));
        assert!(resp.contains("<cwmp:InformResponse>"));
        assert!(resp.contains("<MaxEnvelopes>1</MaxEnvelopes>"));
    }

    #[test]
    fn test_build_reboot_and_get_parameters_rpc() {
        let reboot_xml = generate_reboot_rpc("CMD-REBOOT-01", "reboot-cpe-001");
        assert!(reboot_xml.contains("<cwmp:ID mustUnderstand=\"1\">CMD-REBOOT-01</cwmp:ID>"));
        assert!(reboot_xml.contains("<CommandKey>reboot-cpe-001</CommandKey>"));

        let names = vec![
            "Device.DeviceInfo.SoftwareVersion".to_string(),
            "Device.Optical.Interface.1.OpticalSignalLevel".to_string(),
        ];
        let gpv_xml = generate_get_parameter_values_rpc("CMD-GPV-01", &names);
        assert!(gpv_xml.contains("<cwmp:ID mustUnderstand=\"1\">CMD-GPV-01</cwmp:ID>"));
        assert!(gpv_xml.contains("<ParameterNames soap-enc:arrayType=\"xsd:string[2]\">"));
        assert!(gpv_xml.contains("<string>Device.DeviceInfo.SoftwareVersion</string>"));
        assert!(gpv_xml.contains("<string>Device.Optical.Interface.1.OpticalSignalLevel</string>"));
    }

    #[test]
    fn test_parse_inform_tx_and_ip_and_fault() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID mustUnderstand="1">99999</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>HG8245H</ProductClass>
        <SerialNumber>48575443ABCDEF01</SerialNumber>
      </DeviceId>
      <ParameterList>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value>-18.75 dBm</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalTxPower</Name>
          <Value>2.35 dBm</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.ManagementServer.ConnectionRequestURL</Name>
          <Value>http://172.16.10.50:7547/tr069</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

        let parsed = parse_inform(xml).expect("Parsing with Tx power and ConnectionRequestURL must succeed");
        assert_eq!(parsed.cpe_id, "48575443ABCDEF01");
        assert_eq!(parsed.rx_optical_power, Some(-18.75));
        assert_eq!(parsed.tx_optical_power, Some(2.35));
        assert_eq!(parsed.ip_address, Some("172.16.10.50".to_string()));

        let fault = build_soap_fault("Server", "Internal Server Error", Some("99999"));
        assert!(fault.contains("<faultcode>Server</faultcode>"));
        assert!(fault.contains("<faultstring>Internal Server Error</faultstring>"));
        assert!(fault.contains("<cwmp:ID mustUnderstand=\"1\">99999</cwmp:ID>"));
    }

    #[test]
    fn test_device_id_fallback_when_serial_empty() {
        let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID mustUnderstand="1">333</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>VendorX</Manufacturer>
        <OUI>AABBCC</OUI>
        <ProductClass>ModelY</ProductClass>
        <SerialNumber></SerialNumber>
      </DeviceId>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

        let parsed = parse_inform(xml).expect("Parsing must succeed");
        assert_eq!(parsed.cpe_id, "AABBCC-ModelY-");
    }
}
