//! Empirical Challenger Stress Harness for Milestone 2:
//! Concurrency, MPSC Channel Convergence, Backpressure Saturation Guard & Graceful Shutdown.

#![allow(dead_code)]

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};
use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::mpsc::channel;
use tokio::sync::{watch, RwLock};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
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

/// Helper simulating TR-369 MQTT telemetry generation
fn make_mqtt_telemetry(cpe_id: &str, seq: usize, rx_pwr: f64) -> TelemetryUpdate {
    TelemetryUpdate {
        cpe_id: cpe_id.to_string(),
        endpoint_id: Some(format!("proto::{}", cpe_id)),
        status: "online".to_string(),
        current_parameters: json!({
            "Device.DeviceInfo.SoftwareVersion": "2.1.0-usp",
            "Device.LocalAgent.Sequence": seq
        }),
        telemetry_metrics: json!({
            "rx_optical_power": rx_pwr,
            "cpu_usage": 35.0 + (seq as f64 % 20.0),
            "memory_usage": 60.0
        }),
        ip_address: Some(format!("10.100.{}.{}", (seq / 250) + 1, (seq % 250) + 1)),
        firmware_version: Some("2.1.0-usp".to_string()),
        timestamp: Utc::now(),
    }
}

/// Helper generating standard Huawei TR-069 Inform XML
fn make_cwmp_inform_xml(cpe_id: &str, seq: usize, raw_optical: &str) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID mustUnderstand="1">cwmp-seq-{}</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>Huawei</Manufacturer>
        <OUI>00259E</OUI>
        <ProductClass>HG8245H</ProductClass>
        <SerialNumber>{}</SerialNumber>
      </DeviceId>
      <Event>
        <EventStruct>
          <EventCode>2 PERIODIC</EventCode>
          <CommandKey></CommandKey>
        </EventStruct>
      </Event>
      <ParameterList>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.X_HW_OpticalRxPower</Name>
          <Value>{}</Value>
        </ParameterValueStruct>
        <ParameterValueStruct>
          <Name>InternetGatewayDevice.DeviceInfo.SoftwareVersion</Name>
          <Value>V3R017C10S115</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        seq, cpe_id, raw_optical
    )
}

/// Helper generating TP-Link TR-181 Inform XML
fn make_tplink_inform_xml(cpe_id: &str, seq: usize, millidbm_optical: i64) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:cwmp="urn:dslforum-org:cwmp-1-0">
  <SOAP-ENV:Header><cwmp:ID mustUnderstand="1">tplink-{}</cwmp:ID></SOAP-ENV:Header>
  <SOAP-ENV:Body>
    <cwmp:Inform>
      <DeviceId>
        <Manufacturer>TP-Link</Manufacturer>
        <OUI>F4EC38</OUI>
        <ProductClass>Archer-EX220</ProductClass>
        <SerialNumber>{}</SerialNumber>
      </DeviceId>
      <ParameterList>
        <ParameterValueStruct>
          <Name>Device.Optical.Interface.1.OpticalSignalLevel</Name>
          <Value>{}</Value>
        </ParameterValueStruct>
      </ParameterList>
    </cwmp:Inform>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"#,
        seq, cpe_id, millidbm_optical
    )
}

// -----------------------------------------------------------------------------
// Test 1: Massive Multi-Protocol Concurrent Ingress Convergence
// -----------------------------------------------------------------------------
#[tokio::test]
async fn test_massive_multi_protocol_concurrency_convergence() {
    const NUM_MQTT_PRODUCERS: usize = 25;
    const NUM_CWMP_PRODUCERS: usize = 25;
    const MSGS_PER_PRODUCER: usize = 40;
    const TOTAL_EXPECTED: usize = (NUM_MQTT_PRODUCERS + NUM_CWMP_PRODUCERS) * MSGS_PER_PRODUCER; // 2,000 messages

    let (tx, mut rx) = channel::<TelemetryUpdate>(512);

    let received_count = Arc::new(AtomicUsize::new(0));
    let mqtt_received = Arc::new(AtomicUsize::new(0));
    let cwmp_received = Arc::new(AtomicUsize::new(0));

    // Spawn simulated DB sink drain task
    let rx_counter = Arc::clone(&received_count);
    let rx_mqtt = Arc::clone(&mqtt_received);
    let rx_cwmp = Arc::clone(&cwmp_received);
    let sink_handle = tokio::spawn(async move {
        while let Some(update) = rx.recv().await {
            rx_counter.fetch_add(1, Ordering::SeqCst);
            if update.cpe_id.starts_with("MQTT-") {
                rx_mqtt.fetch_add(1, Ordering::SeqCst);
            } else if update.cpe_id.starts_with("HWTC-") || update.cpe_id.starts_with("TPLK-") {
                rx_cwmp.fetch_add(1, Ordering::SeqCst);
            }
            // Verify structural integrity of each converged item
            assert!(!update.cpe_id.is_empty(), "CPE ID must not be empty");
            assert_eq!(update.status, "online");
            assert!(update.telemetry_metrics.is_object());
        }
    });

    let start_time = Instant::now();
    let mut tasks = Vec::with_capacity(NUM_MQTT_PRODUCERS + NUM_CWMP_PRODUCERS);

    // Spawn 25 concurrent MQTT Producers
    for p in 0..NUM_MQTT_PRODUCERS {
        let p_tx = tx.clone();
        let handle = tokio::spawn(async move {
            let cpe_id = format!("MQTT-CPE-{:03}", p);
            for seq in 0..MSGS_PER_PRODUCER {
                let update = make_mqtt_telemetry(&cpe_id, seq, -18.0 - (p as f64 * 0.1));
                // Match the 500ms timeout send pattern in main.rs
                let res = tokio::time::timeout(Duration::from_millis(500), p_tx.send(update)).await;
                assert!(res.is_ok(), "MQTT send must not timeout under normal load");
                assert!(res.unwrap().is_ok(), "Channel send must succeed");
                // Yield occasionally to maximize interleaving
                if seq % 5 == 0 {
                    tokio::task::yield_now().await;
                }
            }
        });
        tasks.push(handle);
    }

    // Spawn 25 concurrent CWMP Ingest Producers
    for p in 0..NUM_CWMP_PRODUCERS {
        let p_tx = tx.clone();
        let handle = tokio::spawn(async move {
            let is_tplink = p % 2 == 1;
            let cpe_id = if is_tplink {
                format!("TPLK-{:04}", p)
            } else {
                format!("HWTC-{:04}", p)
            };

            for seq in 0..MSGS_PER_PRODUCER {
                let xml = if is_tplink {
                    make_tplink_inform_xml(&cpe_id, seq, -19500)
                } else {
                    make_cwmp_inform_xml(&cpe_id, seq, "-19.50 dBm")
                };

                // Real parsing via cwmp::parse_inform
                let parsed = cwmp::parse_inform(&xml).expect("CWMP Inform XML parse must succeed");
                let client_ip = format!("192.168.1.{}", (p % 250) + 1);
                let update = parsed.into_telemetry_update(Some(client_ip));

                // Match the 500ms timeout send pattern in main.rs
                let res = tokio::time::timeout(Duration::from_millis(500), p_tx.send(update)).await;
                assert!(res.is_ok(), "CWMP send must not timeout under normal load");
                assert!(res.unwrap().is_ok(), "Channel send must succeed");

                if seq % 5 == 0 {
                    tokio::task::yield_now().await;
                }
            }
        });
        tasks.push(handle);
    }

    // Drop original sender so channel can terminate after all tasks finish
    drop(tx);

    // Wait for all producer tasks to complete
    for t in tasks {
        t.await.expect("Producer task panicked");
    }

    // Wait for sink to drain all messages
    sink_handle.await.expect("Sink task panicked");
    let duration = start_time.elapsed();

    let total = received_count.load(Ordering::SeqCst);
    let mqtt_cnt = mqtt_received.load(Ordering::SeqCst);
    let cwmp_cnt = cwmp_received.load(Ordering::SeqCst);

    println!(
        "[STRESS CONCURRENCY PASS] Ingested {} total updates (MQTT: {}, CWMP: {}) in {:?}",
        total, mqtt_cnt, cwmp_cnt, duration
    );

    assert_eq!(total, TOTAL_EXPECTED, "Must receive exactly 2,000 converged messages");
    assert_eq!(mqtt_cnt, NUM_MQTT_PRODUCERS * MSGS_PER_PRODUCER, "MQTT message count mismatch");
    assert_eq!(cwmp_cnt, NUM_CWMP_PRODUCERS * MSGS_PER_PRODUCER, "CWMP message count mismatch");
}

// -----------------------------------------------------------------------------
// Test 2: Channel Saturation & 500ms Backpressure Guard
// -----------------------------------------------------------------------------
#[tokio::test]
async fn test_mpsc_saturation_and_500ms_backpressure_guard() {
    // Channel capacity of 2
    let (tx, _rx) = channel::<TelemetryUpdate>(2);

    // Fill channel to capacity
    let u1 = make_mqtt_telemetry("SAT-001", 1, -19.0);
    let u2 = make_mqtt_telemetry("SAT-002", 2, -19.0);
    tx.try_send(u1).expect("Slot 1 must be available");
    tx.try_send(u2).expect("Slot 2 must be available");

    // Channel is now completely full!
    let u3 = make_mqtt_telemetry("SAT-003", 3, -19.0);

    let start = Instant::now();
    // Simulate the exact backpressure timeout in handle_cwmp_post and run_mqtt_ingest:
    // tokio::time::timeout(Duration::from_millis(500), tx.send(update))
    let send_result = tokio::time::timeout(Duration::from_millis(500), tx.send(u3)).await;
    let elapsed = start.elapsed();

    println!(
        "[STRESS BACKPRESSURE PASS] Saturated channel send timed out after {:?}: {:?}",
        elapsed, send_result
    );

    // Verify 500ms timeout behavior
    assert!(
        elapsed >= Duration::from_millis(480),
        "Timeout must wait at least 500ms (measured {:?})",
        elapsed
    );
    assert!(
        elapsed <= Duration::from_millis(700),
        "Timeout must not hang indefinitely (measured {:?})",
        elapsed
    );
    assert!(send_result.is_err(), "Send must return Err(_) timeout indicating backpressure guard triggered");
}

// -----------------------------------------------------------------------------
// Test 3: Channel Recovery After Saturation Burst
// -----------------------------------------------------------------------------
#[tokio::test]
async fn test_channel_recovery_after_saturation() {
    let (tx, mut rx) = channel::<TelemetryUpdate>(1);

    // Fill channel
    let u1 = make_mqtt_telemetry("REC-001", 1, -19.0);
    tx.try_send(u1).unwrap();

    // Spawn a producer trying to send while full
    let p_tx = tx.clone();
    let producer = tokio::spawn(async move {
        let u2 = make_mqtt_telemetry("REC-002", 2, -19.0);
        // Timeout 1s
        let res = tokio::time::timeout(Duration::from_secs(1), p_tx.send(u2)).await;
        res
    });

    // Receiver waits 100ms then drains slot
    tokio::time::sleep(Duration::from_millis(100)).await;
    let drained_1 = rx.recv().await.expect("Must drain first item");
    assert_eq!(drained_1.cpe_id, "REC-001");

    // The blocked sender must unblock and send its item
    let prod_res = producer.await.expect("Producer task failed");
    assert!(prod_res.is_ok(), "Producer must succeed after receiver frees space");
    assert!(prod_res.unwrap().is_ok());

    let drained_2 = rx.recv().await.expect("Must drain second item");
    assert_eq!(drained_2.cpe_id, "REC-002");
}

// -----------------------------------------------------------------------------
// Test 4: Graceful Shutdown Drain & Zero-Sender-Leak Verification
// -----------------------------------------------------------------------------
#[tokio::test]
async fn test_graceful_shutdown_mpsc_drain_and_no_sender_leak() {
    let (tx, mut rx) = channel::<TelemetryUpdate>(64);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    let drained_items = Arc::new(AtomicUsize::new(0));

    // Simulate DB Sink
    let count_clone = Arc::clone(&drained_items);
    let db_sink_handle = tokio::spawn(async move {
        // When all senders drop, recv() returns None and loop exits
        while let Some(_msg) = rx.recv().await {
            count_clone.fetch_add(1, Ordering::SeqCst);
            // Simulate 2ms Postgres write latency
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
        "sink_clean_exit"
    });

    // Simulate MQTT Ingest holding cloned sender and watching shutdown
    let mqtt_tx = tx.clone();
    let mut mqtt_shutdown = shutdown_rx.clone();
    let mqtt_handle = tokio::spawn(async move {
        let mut seq = 0;
        loop {
            tokio::select! {
                _ = mqtt_shutdown.changed() => {
                    if *mqtt_shutdown.borrow() {
                        break;
                    }
                }
                _ = tokio::time::sleep(Duration::from_millis(5)) => {
                    seq += 1;
                    let u = make_mqtt_telemetry("SHUTDOWN-MQTT", seq, -19.0);
                    if mqtt_tx.send(u).await.is_err() {
                        break;
                    }
                }
            }
        }
        drop(mqtt_tx);
        "mqtt_clean_exit"
    });

    // Simulate CWMP Axum AppState holding cloned sender and watching shutdown
    let cwmp_tx = tx.clone();
    let mut cwmp_shutdown = shutdown_rx.clone();
    let cwmp_handle = tokio::spawn(async move {
        let mut seq = 0;
        loop {
            tokio::select! {
                _ = cwmp_shutdown.changed() => {
                    if *cwmp_shutdown.borrow() {
                        break;
                    }
                }
                _ = tokio::time::sleep(Duration::from_millis(5)) => {
                    seq += 1;
                    let xml = make_cwmp_inform_xml("SHUTDOWN-CWMP", seq, "-19.50 dBm");
                    let parsed = cwmp::parse_inform(&xml).unwrap();
                    let update = parsed.into_telemetry_update(Some("192.168.1.1".to_string()));
                    if cwmp_tx.send(update).await.is_err() {
                        break;
                    }
                }
            }
        }
        drop(cwmp_tx);
        "cwmp_clean_exit"
    });

    // Let the system run for 50ms generating telemetry
    tokio::time::sleep(Duration::from_millis(50)).await;

    // Drop the initial master sender handle (just like main() in main.rs)
    drop(tx);

    // Trigger Graceful Shutdown
    let shutdown_start = Instant::now();
    shutdown_tx.send(true).expect("Shutdown broadcast must succeed");

    // Verify MQTT and CWMP tasks exit cleanly
    let (mqtt_res, cwmp_res) = tokio::join!(mqtt_handle, cwmp_handle);
    assert_eq!(mqtt_res.unwrap(), "mqtt_clean_exit");
    assert_eq!(cwmp_res.unwrap(), "cwmp_clean_exit");

    // The db sink MUST drain remaining items and exit (recv() -> None).
    // If ANY sender was leaked or not dropped, db_sink would hang forever!
    let sink_res = tokio::time::timeout(Duration::from_secs(3), db_sink_handle).await;
    let shutdown_duration = shutdown_start.elapsed();

    assert!(
        sink_res.is_ok(),
        "CRITICAL BUG: DB Sink hung on shutdown! A sender was leaked and never dropped."
    );
    assert_eq!(sink_res.unwrap().unwrap(), "sink_clean_exit");

    let total_drained = drained_items.load(Ordering::SeqCst);
    println!(
        "[STRESS SHUTDOWN PASS] Graceful shutdown completed cleanly in {:?}. Drained all {} items without sender leaks.",
        shutdown_duration, total_drained
    );
    assert!(total_drained > 0, "Must have drained messages");
}

// -----------------------------------------------------------------------------
// Test 5: Session Cache Concurrency & TTL Contention
// -----------------------------------------------------------------------------
#[tokio::test]
async fn test_session_cache_high_contention() {
    let session_cache = Arc::new(RwLock::new(HashMap::new()));

    let mut handles = Vec::new();
    // 20 concurrent readers/writers thrashing session cache
    for t in 0..20 {
        let cache = Arc::clone(&session_cache);
        let handle = tokio::spawn(async move {
            let ip: std::net::IpAddr = format!("192.168.1.{}", t + 1).parse().unwrap();
            let cpe_id = format!("CPE-THREAD-{}", t);

            for i in 0..100 {
                if i % 2 == 0 {
                    // Write
                    let mut w = cache.write().await;
                    w.insert(ip, (cpe_id.clone(), Instant::now()));
                } else {
                    // Read
                    let r = cache.read().await;
                    if let Some((cid, instant)) = r.get(&ip) {
                        assert_eq!(cid, &cpe_id);
                        assert!(instant.elapsed() < Duration::from_secs(10));
                    }
                }
            }
        });
        handles.push(handle);
    }

    for h in handles {
        h.await.expect("Session cache thread failed");
    }

    let final_cache = session_cache.read().await;
    assert_eq!(final_cache.len(), 20, "Session cache must contain all 20 entries");
    println!("[STRESS SESSION CACHE PASS] 20 threads completed 2,000 reads/writes without deadlock");
}

// -----------------------------------------------------------------------------
// Test 6: Optical Normalization Adversarial Stress
// -----------------------------------------------------------------------------
#[test]
fn test_optical_normalization_adversarial_stress() {
    // Extreme values
    assert_eq!(cwmp::parse_optical_power("-19.50"), Some(-19.50));
    assert_eq!(cwmp::parse_optical_power("-19.50 dBm"), Some(-19.50));
    assert_eq!(cwmp::parse_optical_power("-19.50dBm"), Some(-19.50));
    assert_eq!(cwmp::parse_optical_power(" -21.4 dB "), Some(-21.40));
    assert_eq!(cwmp::parse_optical_power("-1950"), Some(-19.50));
    assert_eq!(cwmp::parse_optical_power("-19500"), Some(-19.50));
    assert_eq!(cwmp::parse_optical_power("240"), Some(2.40));
    assert_eq!(cwmp::parse_optical_power("2400"), Some(2.40));
    assert_eq!(cwmp::parse_optical_power("2.40 dBm"), Some(2.40));

    // Edge / Disconnected / Garbage
    assert_eq!(cwmp::parse_optical_power("0"), None);
    assert_eq!(cwmp::parse_optical_power("0.0"), None);
    assert_eq!(cwmp::parse_optical_power("0.00"), None);
    assert_eq!(cwmp::parse_optical_power("N/A"), None);
    assert_eq!(cwmp::parse_optical_power("--"), None);
    assert_eq!(cwmp::parse_optical_power(""), None);
    assert_eq!(cwmp::parse_optical_power("   "), None);
    assert_eq!(cwmp::parse_optical_power("NaN"), None);
    assert_eq!(cwmp::parse_optical_power("Inf"), None);
    assert_eq!(cwmp::parse_optical_power("-9999999"), None);
    assert_eq!(cwmp::parse_optical_power("9999999"), None);
}
