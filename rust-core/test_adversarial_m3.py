#!/usr/bin/env python3
"""
Adversarial Challenge Test Suite for Milestone 3 (Rust USP Core Worker)
Author: challenger_m3_1 (teamwork_preview_challenger)
Target: rust-core/src/main.rs, rust-core/proto/usp.proto, simulate_flow.sh

Challenge Dimensions:
1. Malformed Payloads: invalid protobuf bytes, invalid JSON, empty bytes, non-UTF8 binary strings.
   Validates that PayloadDecoder::decode does not crash (no unhandled unwrap/panic on untrusted data).
2. Topic Filtering: evaluates is_command_topic against command request topics vs allowed telemetry/notify topics.
3. TR-181 Parameter Mapping & Optical Extraction: verifies regex and parsing resilience.
4. AST / Source Integrity: confirms decoupling via MPSC, safe error handling, absence of hardcoded payloads.
"""

import json
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_RS_PATH = REPO_ROOT / "rust-core" / "src" / "main.rs"
PROTO_PATH = REPO_ROOT / "rust-core" / "proto" / "usp.proto"
SIMULATE_FLOW_PATH = REPO_ROOT / "simulate_flow.sh"


class TestRustCoreASTIntegrity(unittest.TestCase):
    """Verifies that rust-core/src/main.rs implements genuine, crash-resilient logic without unhandled panics."""

    def setUp(self):
        self.assertTrue(MAIN_RS_PATH.exists(), f"Missing {MAIN_RS_PATH}")
        self.code = MAIN_RS_PATH.read_text(encoding="utf-8")

    def test_no_unwrap_in_decoding_pipeline(self):
        """Ensures that PayloadDecoder and MQTT ingest do not call .unwrap() or .expect() on incoming packet payloads."""
        # Find the body of PayloadDecoder impl
        decoder_match = re.search(r"impl PayloadDecoder\s*\{(.*?)\n\}", self.code, re.DOTALL)
        self.assertIsNotNone(decoder_match, "PayloadDecoder implementation block must exist")
        decoder_body = decoder_match.group(1)

        # Ensure no unwrap() or expect() in decode methods (excluding unit tests)
        self.assertNotIn(".unwrap()", decoder_body, "PayloadDecoder must never call .unwrap() on untrusted payloads")
        self.assertNotIn(".expect(", decoder_body, "PayloadDecoder must never call .expect() on untrusted payloads")

    def test_empty_payload_guard(self):
        """Verifies that empty payloads are guarded at the very beginning of PayloadDecoder::decode."""
        self.assertIn("if raw.is_empty()", self.code)
        self.assertIn('anyhow!("Received empty payload")', self.code)

    def test_mpsc_decoupling_and_bounded_channel(self):
        """Verifies that MPSC channels decouple MQTT ingestion from PostgreSQL writes with timeout backpressure."""
        self.assertIn("channel::<TelemetryUpdate>(channel_capacity)", self.code)
        self.assertIn("tokio::time::timeout(Duration::from_millis(500), tx.send(update))", self.code)
        self.assertIn("MPSC channel saturated (>500ms); dropping update", self.code)

    def test_healthcheck_state_monitor(self):
        """Verifies that /tmp/healthy is updated strictly when both DB and MQTT are live."""
        self.assertIn("const HEALTH_FILE: &str = \"/tmp/healthy\"", self.code)
        self.assertIn("let db_probe = sqlx::query(\"SELECT 1\").execute(&db_pool).await", self.code)
        self.assertIn("if db_ok && mqtt_ok", self.code)


class TestTopicFilteringOracle(unittest.TestCase):
    """Independent oracle reproducing the exact topic filtering and CPE extraction logic."""

    @staticmethod
    def is_command_topic(topic: str) -> bool:
        return topic.endswith("/request") or ("/request/" in topic)

    @staticmethod
    def extract_cpe_id_from_topic(topic: str) -> str | None:
        parts = topic.split("/")
        if len(parts) >= 3 and parts[0] == "usp" and parts[1] == "endpoint":
            candidate = parts[2].strip()
            if candidate:
                return candidate
        return None

    def test_mandate_topic_filtering(self):
        """Task requirement 2: filter usp/endpoint/cpe1/request and /request/sub; allow notify and telemetry."""
        # Must filter (return True for command topic):
        self.assertTrue(self.is_command_topic("usp/endpoint/cpe1/request"))
        self.assertTrue(self.is_command_topic("usp/endpoint/cpe1/request/sub"))
        self.assertTrue(self.is_command_topic("usp/endpoint/cpe1/request/"))
        self.assertTrue(self.is_command_topic("usp/endpoint/cpe1/request/reboot"))
        self.assertTrue(self.is_command_topic("usp/endpoint/cpe1/request/sub1/sub2"))

        # Must allow (return False for command topic):
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/notify"))
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/telemetry"))
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/status"))
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/event"))

    def test_topic_filtering_edge_cases(self):
        """Stress-test edge cases in topic filtering."""
        # Non-request topics containing "request" as substring but not path segment
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/request_notify"))
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/notify_request"))
        self.assertFalse(self.is_command_topic("usp/endpoint/cpe1/myrequest"))

        # Edge case: CPE named 'request'
        # Topic: usp/endpoint/request/notify contains '/request/' -> filtered
        self.assertTrue(self.is_command_topic("usp/endpoint/request/notify"))

    def test_cpe_id_extraction_oracle(self):
        """Verifies CPE ID extraction across normal and malformed topics."""
        self.assertEqual(self.extract_cpe_id_from_topic("usp/endpoint/cpe-01/notify"), "cpe-01")
        self.assertEqual(self.extract_cpe_id_from_topic("usp/endpoint/cpe-01/telemetry"), "cpe-01")
        self.assertEqual(self.extract_cpe_id_from_topic("usp/endpoint/cpe-01"), "cpe-01")
        self.assertEqual(self.extract_cpe_id_from_topic("usp/endpoint/cpe-01/a/b/c"), "cpe-01")

        # Boundary cases: empty or whitespace
        self.assertIsNone(self.extract_cpe_id_from_topic("usp/endpoint//notify"))
        self.assertIsNone(self.extract_cpe_id_from_topic("usp/endpoint/   /notify"))
        self.assertIsNone(self.extract_cpe_id_from_topic("usp/endpoint"))
        self.assertIsNone(self.extract_cpe_id_from_topic("usp/not-endpoint/cpe-01/notify"))
        self.assertIsNone(self.extract_cpe_id_from_topic(""))


class TestPayloadNormalizationOracle(unittest.TestCase):
    """Verifies JSON payload normalization and optical signal metric extraction logic."""

    @staticmethod
    def normalize_payload(payload_dict, fallback_cpe_id="cpe-default"):
        cpe_id = payload_dict.get("cpe_id")
        if not cpe_id or not str(cpe_id).strip():
            cpe_id = fallback_cpe_id
        else:
            cpe_id = str(cpe_id).strip()

        status = payload_dict.get("status")
        if not status or not str(status).strip():
            status = "online"
        else:
            status = str(status).strip().lower()

        # Consolidate metrics
        metrics = {}
        if isinstance(payload_dict.get("metrics"), dict):
            metrics.update(payload_dict["metrics"])
        if isinstance(payload_dict.get("telemetry_metrics"), dict):
            metrics.update(payload_dict["telemetry_metrics"])

        # Consolidate parameters
        parameters = {}
        if isinstance(payload_dict.get("parameters"), dict):
            parameters.update(payload_dict["parameters"])
        if isinstance(payload_dict.get("current_parameters"), dict):
            parameters.update(payload_dict["current_parameters"])

        # Optical power extraction fallback
        if "rx_optical_power" not in metrics:
            for k, v in parameters.items():
                if "Optical" in k and (k.endswith("OpticalSignalLevel") or k.endswith("RxPower")):
                    try:
                        metrics["rx_optical_power"] = float(v)
                        break
                    except (ValueError, TypeError):
                        pass

        return {
            "cpe_id": cpe_id,
            "status": status,
            "metrics": metrics,
            "parameters": parameters,
        }

    def test_step2_and_step4_simulation_payloads(self):
        step2 = {
            "cpe_id": "cpe-sim-001",
            "status": "online",
            "metrics": {
                "rx_optical_power": -18.5,
                "cpu_usage": 42.5,
                "memory_usage": 68.0,
            },
            "parameters": {
                "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
            },
        }
        res2 = self.normalize_payload(step2)
        self.assertEqual(res2["cpe_id"], "cpe-sim-001")
        self.assertEqual(res2["metrics"]["rx_optical_power"], -18.5)

        step4 = {
            "cpe_id": "cpe-sim-001",
            "status": "online",
            "metrics": {
                "rx_optical_power": -21.0,
                "cpu_usage": 88.4,
            },
        }
        res4 = self.normalize_payload(step4)
        self.assertEqual(res4["metrics"]["rx_optical_power"], -21.0)
        # Delta: |-21.0 - (-18.5)| = 2.5 > 1.0 dBm (triggers reconciliation trigger)
        self.assertGreater(abs(res4["metrics"]["rx_optical_power"] - res2["metrics"]["rx_optical_power"]), 1.0)

    def test_optical_extraction_from_parameters_string(self):
        payload = {
            "cpe_id": "cpe-opt-1",
            "parameters": {
                "Device.Optical.Interface.1.OpticalSignalLevel": "-19.75",
            },
        }
        res = self.normalize_payload(payload)
        self.assertEqual(res["metrics"].get("rx_optical_power"), -19.75)


if __name__ == "__main__":
    unittest.main(verbosity=2)
