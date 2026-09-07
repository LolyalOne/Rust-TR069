//! Empirical XML Stress and Adversarial Test Suite for TR-069 CWMP Parser
//!
//! Evaluates parser resilience against:
//! 1. Completely broken, truncated, corrupted XML
//! 2. Incremental byte-by-byte truncation fuzzing
//! 3. Varied XML namespaces, vendor prefixes, and missing prefix repair
//! 4. Mixed, inverted, out-of-order, and missing tags
//! 5. Extreme, NaN, Inf, zero, disconnected, and out-of-range optical power values
//! 6. Self-closing and empty elements
//! 7. High-stress large payloads (2,000+ parameters)
//! 8. Semantic edge cases (RPC response containing 'Inform' in parameter name)

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value as JsonValue;

// Define TelemetryUpdate so cwmp.rs can resolve crate::TelemetryUpdate
#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[path = "../src/cwmp.rs"]
mod cwmp;

use cwmp::{
    is_optical_power_key, is_optical_rx_power_key, is_optical_tx_power_key,
    normalize_optical_power, parse_inform, parse_optical_power,
};

const SAMPLE_HUAWEI_INFORM: &str = r#"<?xml version="1.0" encoding="UTF-8"?>
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
      </Event>
      <MaxEnvelopes>1</MaxEnvelopes>
      <CurrentTime>2026-09-07T14:30:00Z</CurrentTime>
      <RetryCount>0</RetryCount>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[3]">
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
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

// =============================================================================
// 1. Broken, Malformed, Truncated & Corrupted XML
// =============================================================================

#[test]
fn test_adversarial_completely_broken_xml() {
    let broken_payloads = [
        "",
        "   ",
        "\t\r\n",
        "<",
        "<?xml",
        "<?xml version=\"1.0\"",
        "<SOAP-ENV:Envelope",
        "<SOAP-ENV:Envelope>",
        "<SOAP-ENV:Envelope></WrongTag>",
        "<SOAP-ENV:Envelope><SOAP-ENV:Body></SOAP-ENV:Envelope>",
        "Not XML at all, just plain text payload",
        "{\"json\": \"not xml\"}",
        "<SOAP-ENV:Envelope><SOAP-ENV:Body><cwmp:Inform>",
        "<SOAP-ENV:Envelope><SOAP-ENV:Body><cwmp:Inform></cwmp:Inform></SOAP-ENV:Body></SOAP-ENV:Envelope>",
        "<![CDATA[ unclosed cdata section",
        "<!-- unclosed comment <SOAP-ENV:Envelope>",
        "<SOAP-ENV:Envelope><SOAP-ENV:Body><cwmp:Inform><DeviceId><Manufacturer>Test\x00EmbeddedNull</Manufacturer></DeviceId></cwmp:Inform></SOAP-ENV:Body></SOAP-ENV:Envelope>",
        "<SOAP-ENV:Envelope><SOAP-ENV:Body><cwmp:Inform><DeviceId><Manufacturer>Test\x01\x02Binary</Manufacturer></DeviceId></cwmp:Inform></SOAP-ENV:Body></SOAP-ENV:Envelope>",
    ];

    for (i, payload) in broken_payloads.iter().enumerate() {
        let res = parse_inform(payload);
        assert!(
            res.is_err(),
            "Payload #{i} must return Err but returned Ok: {:?}",
            res
        );
    }
}

#[test]
fn test_adversarial_unescaped_xml_entities() {
    // Unescaped < inside Value text
    let unescaped_lt = r#"<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>HW</Manufacturer><OUI>001</OUI><ProductClass>PC</ProductClass><SerialNumber>SN1</SerialNumber></DeviceId>
      <ParameterList>
        <ParameterValueStruct>
          <Name>Device.Bad</Name>
          <Value>1 < 2</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    assert!(parse_inform(unescaped_lt).is_err());

    // Unescaped & inside Value text
    let unescaped_amp = r#"<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>HW</Manufacturer><OUI>001</OUI><ProductClass>PC</ProductClass><SerialNumber>SN1</SerialNumber></DeviceId>
      <ParameterList>
        <ParameterValueStruct>
          <Name>Device.Bad</Name>
          <Value>foo & bar</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    assert!(parse_inform(unescaped_amp).is_err());
}

// =============================================================================
// 2. Incremental Byte-by-Byte Truncation Fuzzing
// =============================================================================

#[test]
fn test_adversarial_incremental_truncation_fuzzing() {
    let full_xml = SAMPLE_HUAWEI_INFORM;
    let total_len = full_xml.len();

    // Verify baseline passes
    assert!(parse_inform(full_xml).is_ok());

    // Fuzz truncation at every byte boundary
    let mut panic_count = 0;
    for len in 1..total_len {
        let truncated = &full_xml[..len];
        // Ensure slice is on UTF-8 char boundary
        if !full_xml.is_char_boundary(len) {
            continue;
        }

        let res = std::panic::catch_unwind(|| parse_inform(truncated));
        match res {
            Ok(parse_result) => {
                // Truncated XML must fail parsing
                assert!(
                    parse_result.is_err(),
                    "Truncation at len {} unexpectedly succeeded",
                    len
                );
            }
            Err(_) => {
                panic_count += 1;
            }
        }
    }
    assert_eq!(
        panic_count, 0,
        "Incremental truncation caused {} panics!",
        panic_count
    );
}

// =============================================================================
// 3. Varied XML Namespaces, Prefixes & Auto-Repair
// =============================================================================

#[test]
fn test_adversarial_namespace_prefix_variations() {
    // 1. Varied SOAP Envelope prefix (soap: vs soapenv: vs SOAP-ENV:)
    let soap_prefix_xml = r#"<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <soap:Header><cwmp:ID>H-001</cwmp:ID></soap:Header>
  <soap:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>Test</Manufacturer><OUI>001122</OUI><ProductClass>P1</ProductClass><SerialNumber>S1</SerialNumber></DeviceId>
    </cwmp:Inform>
  </soap:Body>
</soap:Envelope>"#;
    let p1 = parse_inform(soap_prefix_xml).expect("soap:Envelope prefix must succeed");
    assert_eq!(p1.header_id, "H-001");
    assert_eq!(p1.cpe_id, "S1");

    // 2. Custom CWMP prefix (tr069: vs cwmp:)
    let custom_cwmp_prefix_xml = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tr069="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><tr069:ID>H-002</tr069:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <tr069:Inform>
      <DeviceId><Manufacturer>Test</Manufacturer><OUI>001122</OUI><ProductClass>P1</ProductClass><SerialNumber>S2</SerialNumber></DeviceId>
    </tr069:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    let p2 = parse_inform(custom_cwmp_prefix_xml).expect("Custom tr069: prefix must succeed");
    assert_eq!(p2.header_id, "H-002");
    assert_eq!(p2.cpe_id, "S2");

    // 3. CWMP 1.2 and CWMP 1.4 namespaces
    let cwmp12_xml = r#"<?xml version="1.0"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-2">
  <soapenv:Header><cwmp:ID>H-003</cwmp:ID></soapenv:Header>
  <soapenv:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>TP-Link</Manufacturer><OUI>EC172F</OUI><ProductClass>EX220</ProductClass><SerialNumber>S3</SerialNumber></DeviceId>
    </cwmp:Inform>
  </soapenv:Body>
</soapenv:Envelope>"#;
    let p3 = parse_inform(cwmp12_xml).expect("CWMP 1.2 namespace must succeed");
    assert_eq!(p3.cpe_id, "S3");

    // 4. Default namespace without any prefixes on elements
    let default_ns_xml = r#"<?xml version="1.0"?>
<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/">
  <Header><ID xmlns="urn:dslforum-org:cwmp-1-0">H-004</ID></Header>
  <Body>
    <Inform xmlns="urn:dslforum-org:cwmp-1-0">
      <DeviceId><Manufacturer>Huawei</Manufacturer><OUI>00259E</OUI><ProductClass>HG</ProductClass><SerialNumber>S4</SerialNumber></DeviceId>
    </Inform>
  </Body>
</Envelope>"#;
    let p4 = parse_inform(default_ns_xml).expect("Default namespace without prefix must succeed");
    assert_eq!(p4.header_id, "H-004");
    assert_eq!(p4.cpe_id, "S4");
}

#[test]
fn test_adversarial_missing_namespace_declarations_auto_repair() {
    // Missing xmlns:cwmp declaration entirely - tests inject_single_namespace
    let missing_cwmp_ns = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Header><cwmp:ID>REPAIRED-01</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>HW</Manufacturer><OUI>001</OUI><ProductClass>PC</ProductClass><SerialNumber>SN-REPAIRED</SerialNumber></DeviceId>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    let res = parse_inform(missing_cwmp_ns);
    assert!(
        res.is_ok(),
        "Parser must auto-repair missing cwmp prefix, but got: {:?}",
        res
    );
    let p = res.unwrap();
    assert_eq!(p.header_id, "REPAIRED-01");
    assert_eq!(p.cpe_id, "SN-REPAIRED");

    // Missing both xmlns:cwmp and xmlns:soap-enc
    let missing_two_ns = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>HW</Manufacturer><OUI>001</OUI><ProductClass>PC</ProductClass><SerialNumber>SN-2NS</SerialNumber></DeviceId>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[1]">
        <ParameterValueStruct>
          <Name>Device.DeviceInfo.SoftwareVersion</Name>
          <Value>1.0</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    let res2 = parse_inform(missing_two_ns);
    assert!(
        res2.is_ok(),
        "Parser must auto-repair multiple missing namespaces: {:?}",
        res2
    );

    // Completely unknown prefix exceeding retry limit (6 unknown prefixes)
    let six_unknown_ns = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <u1:A><u2:B><u3:C><u4:D><u5:E><u6:F>Deep</u6:F></u5:E></u4:D></u3:C></u2:B></u1:A>
      <DeviceId><Manufacturer>HW</Manufacturer><OUI>001</OUI><ProductClass>PC</ProductClass><SerialNumber>SN-FAIL</SerialNumber></DeviceId>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    // Must fail gracefully without panic or infinite loop
    let res_six = parse_inform(six_unknown_ns);
    assert!(res_six.is_err());
}

// =============================================================================
// 4. Mixed, Inverted, Out-of-Order, and Missing Tags
// =============================================================================

#[test]
fn test_adversarial_out_of_order_tags() {
    // ParameterList BEFORE DeviceId
    let params_before_dev_xml = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <ParameterList>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value>-19.50 dBm</Value>
        </ParameterValueStruct>
      </ParameterList>
      <DeviceId>
        <SerialNumber>OUT-OF-ORDER-01</SerialNumber>
        <Manufacturer>Huawei</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>HG8245</ProductClass>
      </DeviceId>
      <Event>
        <EventStruct>
          <EventCode>1 BOOT</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

    let parsed = parse_inform(params_before_dev_xml)
        .expect("ParameterList before DeviceId must parse successfully");
    assert_eq!(parsed.cpe_id, "OUT-OF-ORDER-01");
    assert_eq!(parsed.manufacturer, "Huawei");
    assert_eq!(parsed.rx_optical_power, Some(-19.50));
    assert_eq!(parsed.events.len(), 1);
    assert_eq!(parsed.events[0].event_code, "1 BOOT");
}

#[test]
fn test_adversarial_missing_header_and_device_id_fallbacks() {
    // Inform without SOAP-ENV:Header -> header_id defaults to "1"
    let no_header_xml = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>VendorX</Manufacturer>
        <OUI>AABBCC</OUI>
        <ProductClass>Router1</ProductClass>
        <SerialNumber>SN-NO-HEADER</SerialNumber>
      </DeviceId>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    let p = parse_inform(no_header_xml).expect("Inform without Header must succeed");
    assert_eq!(p.header_id, "1");
    assert_eq!(p.cpe_id, "SN-NO-HEADER");

    // DeviceId with empty elements: <SerialNumber/>, <Manufacturer/>
    let empty_children_dev_xml = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer/>
        <OUI>112233</OUI>
        <ProductClass>ModelZ</ProductClass>
        <SerialNumber></SerialNumber>
      </DeviceId>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    let p2 = parse_inform(empty_children_dev_xml).expect("Empty children in DeviceId must succeed");
    assert_eq!(p2.cpe_id, "112233-ModelZ-");
    assert_eq!(p2.manufacturer, "");

    // DeviceId completely missing
    let no_dev_id_xml = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <ParameterList></ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;
    let res = parse_inform(no_dev_id_xml);
    assert!(res.is_err());
    assert!(res.unwrap_err().to_string().contains("DeviceId"));
}

// =============================================================================
// 5. Self-Closing Tags and Empty Parameter Elements
// =============================================================================

#[test]
fn test_adversarial_self_closing_and_empty_elements() {
    let xml = r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Vendor</Manufacturer>
        <OUI>001122</OUI>
        <ProductClass>PC</ProductClass>
        <SerialNumber>EMPTY-TAGS-CPE</SerialNumber>
      </DeviceId>
      <ParameterList>
        <!-- Self closing Value -->
        <ParameterValueStruct>
          <Name>Device.SelfClosingVal</Name>
          <Value/>
        </ParameterValueStruct>
        <!-- Self closing Value with xsi:type -->
        <ParameterValueStruct>
          <Name>Device.SelfClosingValTyped</Name>
          <Value xsi:type="xsd:string"/>
        </ParameterValueStruct>
        <!-- Empty Value tag -->
        <ParameterValueStruct>
          <Name>Device.EmptyVal</Name>
          <Value></Value>
        </ParameterValueStruct>
        <!-- Whitespace only Value tag -->
        <ParameterValueStruct>
          <Name>Device.WhitespaceVal</Name>
          <Value>   </Value>
        </ParameterValueStruct>
        <!-- Missing Name element - should be skipped -->
        <ParameterValueStruct>
          <Value>no-name</Value>
        </ParameterValueStruct>
        <!-- Empty Name element - should be skipped -->
        <ParameterValueStruct>
          <Name></Name>
          <Value>empty-name</Value>
        </ParameterValueStruct>
        <!-- Missing Value element -->
        <ParameterValueStruct>
          <Name>Device.MissingValue</Name>
        </ParameterValueStruct>
        <!-- Empty struct -->
        <ParameterValueStruct/>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

    let p = parse_inform(xml).expect("Empty & self-closing tags must parse cleanly");
    assert_eq!(p.cpe_id, "EMPTY-TAGS-CPE");

    assert_eq!(
        p.parameters.get("Device.SelfClosingVal").and_then(|v| v.as_str()),
        Some("")
    );
    assert_eq!(
        p.parameters.get("Device.SelfClosingValTyped").and_then(|v| v.as_str()),
        Some("")
    );
    assert_eq!(
        p.parameters.get("Device.EmptyVal").and_then(|v| v.as_str()),
        Some("")
    );
    assert_eq!(
        p.parameters.get("Device.WhitespaceVal").and_then(|v| v.as_str()),
        Some("")
    );
    assert_eq!(
        p.parameters.get("Device.MissingValue").and_then(|v| v.as_str()),
        Some("")
    );

    // Verify invalid structs were skipped
    assert!(!p.parameters.contains_key(""));
    assert_eq!(p.parameters.len(), 5);
}

// =============================================================================
// 6. Extreme Optical Power Values, Boundaries & Scaling Contract
// =============================================================================

#[test]
fn test_adversarial_optical_power_non_numeric_and_special_values() {
    let rejected_cases = [
        "",
        "   ",
        "N/A",
        "n/a",
        "N/a",
        "--",
        "-",
        "+",
        "0",
        "0.0",
        "0.00",
        "-0",
        "+0",
        "0 dBm",
        "0.0 dBm",
        "0.00 dBm",
        "NaN",
        "nan",
        "Inf",
        "-Inf",
        "+Inf",
        "infinity",
        "-19.5.5",
        "-. dBm",
        "-.-",
        "--19.5",
        "-+19.5",
        "12-34",
        "abc",
        "null",
        "undefined",
        "LOS",
        "Loss of Signal",
    ];

    for val in rejected_cases {
        assert_eq!(
            parse_optical_power(val),
            None,
            "Optical power value {:?} must be parsed as None",
            val
        );
    }
}

#[test]
fn test_adversarial_optical_power_extreme_decimals_and_boundaries() {
    // Valid physical GPON dynamic range: [-60.0, 30.0]
    assert_eq!(parse_optical_power("-60.0"), Some(-60.0));
    assert_eq!(parse_optical_power("-60.00"), Some(-60.0));
    assert_eq!(parse_optical_power("30.0"), Some(30.0));
    assert_eq!(parse_optical_power("+30.00"), Some(30.0));
    assert_eq!(parse_optical_power("-19.50"), Some(-19.50));
    assert_eq!(parse_optical_power("2.50"), Some(2.50));

    // Decimal out-of-range: must be None
    assert_eq!(parse_optical_power("-60.01"), None);
    assert_eq!(parse_optical_power("-60.1"), None);
    assert_eq!(parse_optical_power("-100.0"), None);
    assert_eq!(parse_optical_power("-999.0"), None);
    assert_eq!(parse_optical_power("30.01"), None);
    assert_eq!(parse_optical_power("30.1"), None);
    assert_eq!(parse_optical_power("+100.0"), None);
    assert_eq!(parse_optical_power("+999.0"), None);
}

#[test]
fn test_adversarial_optical_power_scaled_integers_range_checking() {
    // Standard vendor scales:
    // Unscaled coarse:
    assert_eq!(parse_optical_power("-19"), Some(-19.0));
    assert_eq!(parse_optical_power("-60"), Some(-60.0));
    assert_eq!(parse_optical_power("2"), Some(2.0));
    assert_eq!(parse_optical_power("30"), Some(30.0));

    // Huawei / ZTE 0.01 dBm scale:
    assert_eq!(parse_optical_power("-1950"), Some(-19.50));
    assert_eq!(parse_optical_power("-2730"), Some(-27.30));
    assert_eq!(parse_optical_power("240"), Some(2.40));

    // TP-Link TR-181 0.001 dBm scale:
    assert_eq!(parse_optical_power("-19500"), Some(-19.50));
    assert_eq!(parse_optical_power("-21300"), Some(-21.30));
    assert_eq!(parse_optical_power("2400"), Some(2.40));

    // CRITICAL EMPIRICAL TEST: Out-of-range integer values
    // Physical GPON/EPON dynamic range contract: [-60.0, +30.0] dBm.
    // What happens when vendor integer is -9999 (-99.99 dBm) or -99999 (-99.999 dBm) or 50000 (+50.0 dBm)?
    let p_neg99 = parse_optical_power("-9999");
    let p_neg99k = parse_optical_power("-99999");
    let p_pos50k = parse_optical_power("50000");

    eprintln!(
        "[EMPIRICAL OBSERVATION] parse_optical_power(\"-9999\") = {:?}",
        p_neg99
    );
    eprintln!(
        "[EMPIRICAL OBSERVATION] parse_optical_power(\"-99999\") = {:?}",
        p_neg99k
    );
    eprintln!(
        "[EMPIRICAL OBSERVATION] parse_optical_power(\"50000\") = {:?}",
        p_pos50k
    );

    // Large overflow numbers must not panic
    assert_eq!(parse_optical_power("9999999999999999999999999999999999999999"), None);
    assert_eq!(parse_optical_power("-9999999999999999999999999999999999999999"), None);
}

#[test]
fn test_adversarial_optical_key_discrimination() {
    // Optical Rx power keys
    assert!(is_optical_rx_power_key(
        "InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower"
    ));
    assert!(is_optical_rx_power_key(
        "Device.Optical.Interface.1.OpticalSignalLevel"
    ));
    assert!(is_optical_rx_power_key(
        "InternetGatewayDevice.WANDevice.1.WANEponInterfaceConfig.RxPower"
    ));
    assert!(is_optical_rx_power_key("Device.GPON.RxPower"));

    // Optical Tx power keys MUST NOT match Rx power
    assert!(!is_optical_rx_power_key(
        "InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalTxPower"
    ));
    assert!(!is_optical_rx_power_key(
        "Device.Optical.Interface.1.TransmitOpticalLevel"
    ));
    assert!(!is_optical_rx_power_key(
        "InternetGatewayDevice.WANDevice.1.WANEponInterfaceConfig.TxPower"
    ));
    assert!(is_optical_tx_power_key(
        "InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalTxPower"
    ));
    assert!(is_optical_tx_power_key(
        "Device.Optical.Interface.1.TransmitOpticalLevel"
    ));

    // Both are recognized by is_optical_power_key
    assert!(is_optical_power_key("Device.Optical.Interface.1.OpticalSignalLevel"));
    assert!(is_optical_power_key("Device.Optical.Interface.1.TransmitOpticalLevel"));

    // Non-optical keys MUST NOT match
    assert!(!is_optical_rx_power_key("Device.DeviceInfo.SoftwareVersion"));
    assert!(!is_optical_tx_power_key("Device.DeviceInfo.SoftwareVersion"));
    assert!(!is_optical_power_key("Device.DeviceInfo.SoftwareVersion"));
    assert!(!is_optical_power_key("Device.WiFi.Radio.1.Status"));

    // normalize_optical_power with non-optical key must return None even if value looks numeric
    assert_eq!(
        normalize_optical_power("Device.DeviceInfo.HardwareVersion", "-19.50 dBm"),
        None
    );
}

// =============================================================================
// 7. High-Stress Large Payloads (Thousands of Parameters)
// =============================================================================

#[test]
fn test_adversarial_large_payload_stress() {
    let mut large_xml = String::with_capacity(500_000);
    large_xml.push_str(r#"<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID>STRESS-01</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId><Manufacturer>StressTest</Manufacturer><OUI>123456</OUI><ProductClass>StressBox</ProductClass><SerialNumber>STRESS-CPE-001</SerialNumber></DeviceId>
      <ParameterList>
"#);

    for i in 0..2000 {
        large_xml.push_str(&format!(
            "        <ParameterValueStruct><Name>Device.TestParameter.{}</Name><Value>Value_{}</Value></ParameterValueStruct>\n",
            i, i
        ));
    }

    large_xml.push_str(r#"      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#);

    let start = std::time::Instant::now();
    let parsed = parse_inform(&large_xml).expect("2000-parameter XML must parse successfully");
    let elapsed = start.elapsed();

    assert_eq!(parsed.cpe_id, "STRESS-CPE-001");
    assert_eq!(parsed.parameters.len(), 2000);
    eprintln!(
        "[EMPIRICAL PERFORMANCE] 2000-parameter XML parsed in {:?}",
        elapsed
    );
    assert!(
        elapsed < std::time::Duration::from_millis(500),
        "Parsing 2000 parameters took too long: {:?}",
        elapsed
    );
}

// =============================================================================
// 8. Semantic Edge Case: RPC Response containing 'Inform' in Parameter Name
// =============================================================================

#[test]
fn test_adversarial_rpc_response_with_inform_in_parameter_name() {
    // In TR-069, GetParameterValuesResponse frequently contains PeriodicInformInterval
    let gpv_response_xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope 
    xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" 
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" 
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" 
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" 
    xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header>
    <cwmp:ID mustUnderstand="1">CMD-GPV-01</cwmp:ID>
  </SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:GetParameterValuesResponse>
      <ParameterList soap-enc:arrayType="cwmp:ParameterValueStruct[2]">
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.ManagementServer.PeriodicInformInterval</Name>
          <Value xsi:type="xsd:unsignedInt">300</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.ManagementServer.PeriodicInformEnable</Name>
          <Value xsi:type="xsd:boolean">1</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:GetParameterValuesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#;

    // Verify parse_inform rejects this response cleanly with Missing <cwmp:Inform>
    let res = parse_inform(gpv_response_xml);
    assert!(
        res.is_err(),
        "parse_inform must fail on GetParameterValuesResponse"
    );
    let err_msg = res.unwrap_err().to_string();
    assert!(
        err_msg.contains("Missing <cwmp:Inform>"),
        "Expected missing Inform error, got: {}",
        err_msg
    );
}
