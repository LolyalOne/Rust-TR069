"""
Unit and integration tests for TR-369 USP ACS Manager FastAPI application.
Verifies all routes, schema serialization, database operations, and MQTT command formatting.
"""

import json
import re
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import CpeHistoricalMetrics, CpeInventory, CpeLiveState
from app.mqtt import MqttPublisher, mqtt_publisher

try:
    from tests.conftest import TestingSessionLocal
except ImportError:
    from conftest import TestingSessionLocal


# -----------------------------------------------------------------------------
# Healthcheck Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_check_endpoints(client: AsyncClient):
    """Verifies that both /health and /api/v1/health return 200 OK and valid JSON."""
    with patch("app.routers.health.AsyncSessionLocal", TestingSessionLocal):
        # Test root /health
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"
        assert "mqtt_broker" in data

        # Test prefixed /api/v1/health
        resp_v1 = await client.get("/api/v1/health")
        assert resp_v1.status_code == 200
        data_v1 = resp_v1.json()
        assert data_v1["status"] == "healthy"


# -----------------------------------------------------------------------------
# CPE Registration & Inventory CRUD Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cpe_registration_lifecycle(client: AsyncClient):
    """
    Tests complete lifecycle:
    1. POST /api/v1/cpes -> creates CPE with HTTP 201
    2. GET /api/v1/cpes/{cpe_id} -> retrieves CPE details
    3. PUT /api/v1/cpes/{cpe_id} -> updates CPE details
    4. POST /api/v1/cpes -> re-registers/upserts with HTTP 200
    5. GET /api/v1/cpes -> lists CPEs
    6. DELETE /api/v1/cpes/{cpe_id} -> removes CPE with HTTP 200
    7. GET /api/v1/cpes/{cpe_id} -> returns 404
    """
    cpe_payload = {
        "cpe_id": "cpe-sim-001",
        "serial_number": "SN-TEST-12345",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
        "oui": "00259E",
        "product_class": "Gateway",
        "hardware_version": "v1.0",
        "software_version": "1.0.0",
        "description": "E2E Automated Simulation Device",
    }

    # 1. Create CPE
    resp = await client.post("/api/v1/cpes", json=cpe_payload)
    assert resp.status_code == 201
    reg_data = resp.json()
    assert reg_data["cpe_id"] == "cpe-sim-001"
    assert reg_data["model"] == "Archer-AX50"
    assert reg_data["status"] == "offline"

    # 2. Get CPE
    resp_get = await client.get("/api/v1/cpes/cpe-sim-001")
    assert resp_get.status_code == 200
    assert resp_get.json()["model"] == "Archer-AX50"

    # 3. Update CPE
    update_payload = {"description": "Updated Description", "status": "online"}
    resp_upd = await client.put("/api/v1/cpes/cpe-sim-001", json=update_payload)
    assert resp_upd.status_code == 200
    assert resp_upd.json()["description"] == "Updated Description"
    assert resp_upd.json()["status"] == "online"

    # 4. Upsert / Re-register same cpe_id (returns HTTP 200)
    cpe_payload["description"] = "Re-registered via API"
    resp_upsert = await client.post("/api/v1/cpes", json=cpe_payload)
    assert resp_upsert.status_code == 200
    assert resp_upsert.json()["description"] == "Re-registered via API"

    # 5. List CPEs
    resp_list = await client.get("/api/v1/cpes")
    assert resp_list.status_code == 200
    cpe_list = resp_list.json()
    assert len(cpe_list) >= 1
    assert any(c["cpe_id"] == "cpe-sim-001" for c in cpe_list)

    # 6. Delete CPE
    resp_del = await client.delete("/api/v1/cpes/cpe-sim-001")
    assert resp_del.status_code == 200
    assert resp_del.json() == {"status": "deleted", "cpe_id": "cpe-sim-001"}

    # 7. Confirm 404 after delete
    resp_after = await client.get("/api/v1/cpes/cpe-sim-001")
    assert resp_after.status_code == 404


@pytest.mark.asyncio
async def test_cpe_conflict_on_duplicate_serial(client: AsyncClient):
    """Verifies that attempting to register a duplicate serial number returns 409 Conflict."""
    cpe1 = {
        "cpe_id": "cpe-001",
        "serial_number": "UNIQUE-SN-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    }
    cpe2 = {
        "cpe_id": "cpe-002",
        "serial_number": "UNIQUE-SN-001",  # Same serial number, different cpe_id
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    }

    resp1 = await client.post("/api/v1/cpes", json=cpe1)
    assert resp1.status_code == 201

    resp2 = await client.post("/api/v1/cpes", json=cpe2)
    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"]


# -----------------------------------------------------------------------------
# Live State Endpoint Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cpe_live_state(client: AsyncClient):
    """
    Verifies that /api/v1/cpes/{cpe_id}/live-state (and /live):
    - returns 404 when CPE does not exist
    - returns 404 when CPE exists but live state has not yet been reported
    - returns 200 with both telemetry_metrics & metrics aliases, and current_parameters & parameters aliases
    """
    # 1. Unknown CPE -> 404
    resp_unknown = await client.get("/api/v1/cpes/non-existent/live-state")
    assert resp_unknown.status_code == 404

    # 2. Register CPE
    cpe_payload = {
        "cpe_id": "cpe-live-001",
        "serial_number": "SN-LIVE-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    }
    await client.post("/api/v1/cpes", json=cpe_payload)

    # 3. Live state not yet reported -> 404
    resp_not_yet = await client.get("/api/v1/cpes/cpe-live-001/live-state")
    assert resp_not_yet.status_code == 404
    assert "not available yet" in resp_not_yet.json()["detail"]

    # 4. Insert live state directly into database (simulating Rust Core worker ingest)
    async with TestingSessionLocal() as session:
        live = CpeLiveState(
            cpe_id="cpe-live-001",
            endpoint_id="proto::cpe-live-001",
            status="online",
            telemetry_metrics={
                "rx_optical_power": -18.5,
                "cpu_usage": 42.5,
                "memory_usage": 68.0,
            },
            current_parameters={
                "Device.DeviceInfo.SoftwareVersion": "1.2.0-prod",
                "Device.WiFi.Radio.1.Status": "Up",
            },
            ip_address="192.168.1.100",
            firmware_version="1.2.0",
        )
        session.add(live)
        await session.commit()

    # 5. Query /live-state
    resp_live = await client.get("/api/v1/cpes/cpe-live-001/live-state")
    assert resp_live.status_code == 200
    data = resp_live.json()

    assert data["cpe_id"] == "cpe-live-001"
    assert data["status"] == "online"
    # Verification matching simulate_flow.sh requirements:
    assert data["telemetry_metrics"]["cpu_usage"] == 42.5
    assert data["metrics"]["cpu_usage"] == 42.5
    assert data["current_parameters"]["Device.WiFi.Radio.1.Status"] == "Up"
    assert data["parameters"]["Device.WiFi.Radio.1.Status"] == "Up"

    # 6. Query alternative endpoint /live
    resp_alt = await client.get("/api/v1/cpes/cpe-live-001/live")
    assert resp_alt.status_code == 200
    assert resp_alt.json()["status"] == "online"
    assert resp_alt.json()["metrics"]["cpu_usage"] == 42.5


# -----------------------------------------------------------------------------
# Historical Metrics Endpoint Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cpe_history(client: AsyncClient):
    """
    Verifies that /api/v1/cpes/{cpe_id}/history:
    - returns 404 for unknown CPE
    - returns items list with count >= 2 when multiple historical snapshots exist
    - verifies fields: cpe_id, total, items with optical_power and change_reason
    """
    # 1. Unknown CPE -> 404
    resp_unknown = await client.get("/api/v1/cpes/cpe-hist-unknown/history")
    assert resp_unknown.status_code == 404

    # 2. Register CPE
    cpe_payload = {
        "cpe_id": "cpe-hist-001",
        "serial_number": "SN-HIST-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    }
    await client.post("/api/v1/cpes", json=cpe_payload)

    # 3. Initially empty history
    resp_empty = await client.get("/api/v1/cpes/cpe-hist-001/history")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["total"] == 0
    assert resp_empty.json()["items"] == []

    # 4. Insert 2 historical snapshots (simulating reconciliation trigger firing)
    async with TestingSessionLocal() as session:
        h1 = CpeHistoricalMetrics(
            cpe_id="cpe-hist-001",
            status="online",
            telemetry_metrics={"rx_optical_power": -18.5, "cpu_usage": 42.5},
            optical_power=Decimal("-18.50"),
            change_reason="initial_state",
        )
        h2 = CpeHistoricalMetrics(
            cpe_id="cpe-hist-001",
            status="online",
            telemetry_metrics={"rx_optical_power": -21.0, "cpu_usage": 88.4},
            optical_power=Decimal("-21.00"),
            change_reason="optical_signal_variation",
        )
        session.add_all([h1, h2])
        await session.commit()

    # 5. Query /history
    resp_hist = await client.get("/api/v1/cpes/cpe-hist-001/history")
    assert resp_hist.status_code == 200
    hist_data = resp_hist.json()

    assert hist_data["cpe_id"] == "cpe-hist-001"
    assert hist_data["total"] == 2
    assert len(hist_data["items"]) == 2
    # Verify optical power and change reasons
    reasons = [item["change_reason"] for item in hist_data["items"]]
    assert "initial_state" in reasons
    assert "optical_signal_variation" in reasons


# -----------------------------------------------------------------------------
# Reboot Command Dispatch & MQTT Wire Formatting Tests
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reboot_command_dispatch_and_mqtt_payload(client: AsyncClient):
    """
    Verifies that POST /api/v1/cpes/{cpe_id}/reboot:
    1. Returns 404 for unknown CPE
    2. Publishes TR-369 Reboot Operate command to Mosquitto topic usp/endpoint/{cpe_id}/request with QoS 1
    3. Payload conforms to BBF TR-369 USP Operate and matches regex 'reboot|operate'
    4. Handles MQTT publisher failures with HTTP 503
    """
    # 1. Unknown CPE -> 404
    resp_unknown = await client.post("/api/v1/cpes/cpe-cmd-unknown/reboot")
    assert resp_unknown.status_code == 404

    # 2. Register CPE
    cpe_payload = {
        "cpe_id": "cpe-cmd-001",
        "serial_number": "SN-CMD-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    }
    await client.post("/api/v1/cpes", json=cpe_payload)

    # 3. Test successful reboot dispatch with captured payload
    captured_topic = None
    captured_payload = None
    captured_qos = None

    async def mock_publish(topic: str, payload: str | bytes, qos: int = 1):
        nonlocal captured_topic, captured_payload, captured_qos
        captured_topic = topic
        captured_payload = payload if isinstance(payload, str) else payload.decode("utf-8")
        captured_qos = qos

    with patch.object(mqtt_publisher, "publish", side_effect=mock_publish):
        resp_reboot = await client.post("/api/v1/cpes/cpe-cmd-001/reboot")
        assert resp_reboot.status_code in (200, 202)
        cmd_data = resp_reboot.json()

        assert cmd_data["status"] == "dispatched"
        assert cmd_data["cpe_id"] == "cpe-cmd-001"
        assert cmd_data["command"] == "Device.Reboot()"
        assert cmd_data["topic"] == "usp/endpoint/cpe-cmd-001/request"

        # Assert MQTT publishing parameters
        assert captured_topic == "usp/endpoint/cpe-cmd-001/request"
        assert captured_qos == 1
        assert captured_payload is not None

        # Assert payload contains both 'reboot' and 'operate' (matching simulate_flow.sh regex)
        assert re.search(r"reboot|operate", captured_payload, re.IGNORECASE)

        # Assert JSON payload structure
        parsed_json = json.loads(captured_payload)
        assert parsed_json["command"] == "Device.Reboot()"
        assert parsed_json["cpe_id"] == "cpe-cmd-001"
        assert parsed_json["operate"]["command"] == "Device.Reboot()"
        assert parsed_json["usp"]["body"]["request"]["operate"]["command"] == "Device.Reboot()"

    # 4. Test MQTT failure -> HTTP 503
    async def mock_publish_failure(topic: str, payload: str | bytes, qos: int = 1):
        raise ConnectionRefusedError("Mosquitto broker connection failed")

    with patch.object(mqtt_publisher, "publish", side_effect=mock_publish_failure):
        resp_fail = await client.post("/api/v1/cpes/cpe-cmd-001/reboot")
        assert resp_fail.status_code == 503
        assert "MQTT broker delivery failed" in resp_fail.json()["detail"]


# -----------------------------------------------------------------------------
# Cascade Deletion Verification
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cascade_deletion(client: AsyncClient):
    """Verifies that deleting a CPE cascades deletion to live state and history in database."""
    # 1. Register CPE
    cpe_payload = {
        "cpe_id": "cpe-cascade-001",
        "serial_number": "SN-CASCADE-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    }
    await client.post("/api/v1/cpes", json=cpe_payload)

    # 2. Add live state and historical entry
    async with TestingSessionLocal() as session:
        live = CpeLiveState(
            cpe_id="cpe-cascade-001",
            status="online",
            telemetry_metrics={"cpu_usage": 50.0},
        )
        hist = CpeHistoricalMetrics(
            cpe_id="cpe-cascade-001",
            status="online",
            telemetry_metrics={"cpu_usage": 50.0},
            change_reason="initial_state",
        )
        session.add_all([live, hist])
        await session.commit()

    # 3. Delete CPE via API
    resp_del = await client.delete("/api/v1/cpes/cpe-cascade-001")
    assert resp_del.status_code == 200

    # 4. Verify in DB that live state and history records are gone
    async with TestingSessionLocal() as session:
        lives = (await session.execute(select(CpeLiveState).where(CpeLiveState.cpe_id == "cpe-cascade-001"))).scalars().all()
        hists = (await session.execute(select(CpeHistoricalMetrics).where(CpeHistoricalMetrics.cpe_id == "cpe-cascade-001"))).scalars().all()
        assert len(lives) == 0
        assert len(hists) == 0


@pytest.mark.asyncio
async def test_cpe_list_pagination_and_filtering(client: AsyncClient):
    """Verifies pagination (skip, limit) and status filtering on GET /api/v1/cpes."""
    for i in range(5):
        cpe = {
            "cpe_id": f"cpe-page-{i}",
            "serial_number": f"SN-PAGE-{i}",
            "manufacturer": "TP-Link",
            "model": "Archer-AX50",
        }
        await client.post("/api/v1/cpes", json=cpe)

    # Set status of cpe-page-0 to 'online'
    await client.put("/api/v1/cpes/cpe-page-0", json={"status": "online"})

    # Test limit = 2
    resp_limit = await client.get("/api/v1/cpes?skip=0&limit=2")
    assert resp_limit.status_code == 200
    assert len(resp_limit.json()) == 2

    # Test filter by status=online
    resp_filter = await client.get("/api/v1/cpes?status=online")
    assert resp_filter.status_code == 200
    online_cpes = resp_filter.json()
    assert len(online_cpes) == 1
    assert online_cpes[0]["cpe_id"] == "cpe-page-0"


@pytest.mark.asyncio
async def test_cpe_validation_error(client: AsyncClient):
    """Verifies that invalid payloads trigger standard 422 Unprocessable Entity."""
    # Missing required field 'model'
    resp = await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-invalid",
        "serial_number": "SN-INVALID",
        "manufacturer": "TP-Link",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_mqtt_publisher_initialization():
    """Verifies MqttPublisher initialization with default parameters."""
    pub = MqttPublisher()
    assert pub.host == "mosquitto"
    assert pub.port == 1883
    assert "fastapi-manager" in pub.client_id

