"""
Empirical Adversarial Challenge Suite for TR-369 USP ACS FastAPI Manager.
Tests edge cases, boundary conditions, error handling, cascade deletions,
MQTT wire-format validation, and stress scenarios.
"""

import asyncio
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
from app.mqtt import mqtt_publisher

try:
    from tests.conftest import TestingSessionLocal
except ImportError:
    from conftest import TestingSessionLocal


# =============================================================================
# 1. ERROR HANDLING: 404 NOT FOUND EXHAUSTIVE SWEEP
# =============================================================================

@pytest.mark.asyncio
async def test_404_on_all_endpoints_with_nonexistent_cpe(client: AsyncClient):
    """
    Asserts that every endpoint operating on a CPE returns HTTP 404
    when target CPE ID does not exist in inventory.
    """
    fake_id = "cpe-nonexistent-999"

    # GET inventory
    r_get = await client.get(f"/api/v1/cpes/{fake_id}")
    assert r_get.status_code == 404, f"Expected 404 on GET, got {r_get.status_code}"

    # PUT inventory
    r_put = await client.put(f"/api/v1/cpes/{fake_id}", json={"description": "new"})
    assert r_put.status_code == 404, f"Expected 404 on PUT, got {r_put.status_code}"

    # PATCH inventory
    r_patch = await client.patch(f"/api/v1/cpes/{fake_id}", json={"status": "online"})
    assert r_patch.status_code == 404, f"Expected 404 on PATCH, got {r_patch.status_code}"

    # DELETE inventory
    r_del = await client.delete(f"/api/v1/cpes/{fake_id}")
    assert r_del.status_code == 404, f"Expected 404 on DELETE, got {r_del.status_code}"

    # GET live-state
    r_live = await client.get(f"/api/v1/cpes/{fake_id}/live-state")
    assert r_live.status_code == 404, f"Expected 404 on GET live-state, got {r_live.status_code}"

    # GET live (alias)
    r_live_alias = await client.get(f"/api/v1/cpes/{fake_id}/live")
    assert r_live_alias.status_code == 404, f"Expected 404 on GET live alias, got {r_live_alias.status_code}"

    # GET history
    r_hist = await client.get(f"/api/v1/cpes/{fake_id}/history")
    assert r_hist.status_code == 404, f"Expected 404 on GET history, got {r_hist.status_code}"

    # POST reboot
    r_reboot = await client.post(f"/api/v1/cpes/{fake_id}/reboot")
    assert r_reboot.status_code == 404, f"Expected 404 on POST reboot, got {r_reboot.status_code}"


@pytest.mark.asyncio
async def test_404_live_state_when_inventory_exists_but_no_telemetry_reported(client: AsyncClient):
    """
    Asserts 404 specifically when CPE is registered in inventory
    but hasn't sent telemetry to cpe_live_state yet.
    """
    cpe_id = "cpe-pending-telemetry"
    r_reg = await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-PENDING-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_reg.status_code == 201

    r_live = await client.get(f"/api/v1/cpes/{cpe_id}/live-state")
    assert r_live.status_code == 404
    assert "not available yet" in r_live.json()["detail"]


@pytest.mark.asyncio
async def test_cpe_id_special_formats_and_boundaries(client: AsyncClient):
    """
    Tests handling of complex CPE IDs (URN, colons, maximum length 128).
    """
    urn_id = "urn:bbf:usp:cpe:00259e-123456"
    r_reg = await client.post("/api/v1/cpes", json={
        "cpe_id": urn_id,
        "serial_number": "SN-URN-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_reg.status_code == 201
    assert r_reg.json()["cpe_id"] == urn_id

    r_get = await client.get(f"/api/v1/cpes/{urn_id}")
    assert r_get.status_code == 200
    assert r_get.json()["cpe_id"] == urn_id

    # 128 chars boundary
    len_128_id = "c" * 128
    r_128 = await client.post("/api/v1/cpes", json={
        "cpe_id": len_128_id,
        "serial_number": "SN-128-LEN",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_128.status_code == 201

    # 129 chars exceeds max_length
    len_129_id = "c" * 129
    r_129 = await client.post("/api/v1/cpes", json={
        "cpe_id": len_129_id,
        "serial_number": "SN-129-LEN",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_129.status_code == 422


# =============================================================================
# 2. ERROR HANDLING: 409 CONFLICT ON DUPLICATE SERIAL NUMBER
# =============================================================================

@pytest.mark.asyncio
async def test_409_on_duplicate_serial_for_new_cpe(client: AsyncClient):
    """
    Creating a new CPE with a serial_number already registered must return 409.
    """
    await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-alpha",
        "serial_number": "SN-SHARED-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })

    r_dup = await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-beta",
        "serial_number": "SN-SHARED-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_dup.status_code == 409
    assert "already exists" in r_dup.json()["detail"]


@pytest.mark.asyncio
async def test_duplicate_serial_on_reregistration_upsert(client: AsyncClient):
    """
    ADVERSARIAL STRESS TEST:
    If cpe-2 already exists with SN-2, and client sends a POST to update cpe-2
    with SN-1 (which belongs to cpe-1), the API should reject with 409 Conflict,
    NOT crash with an unhandled 500 error.
    """
    r_dev1 = await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-dev-1",
        "serial_number": "SN-DEV-1",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_dev1.status_code == 201

    r_dev2 = await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-dev-2",
        "serial_number": "SN-DEV-2",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    assert r_dev2.status_code == 201

    # Try to re-register cpe-dev-2 with cpe-dev-1's serial number
    r_conflict = await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-dev-2",
        "serial_number": "SN-DEV-1",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    # We record what status code is returned: 409 vs 500
    assert r_conflict.status_code == 409, (
        f"Expected 409 Conflict on duplicate serial during re-registration, but got {r_conflict.status_code} "
        f"Detail: {r_conflict.text}"
    )


# =============================================================================
# 3. ERROR HANDLING: 503 SERVICE UNAVAILABLE ON MQTT FAILURE
# =============================================================================

@pytest.mark.asyncio
async def test_503_on_mqtt_broker_failure(client: AsyncClient):
    """
    Verifies that broker unavailability (connection refused, timeout, network error)
    produces HTTP 503 instead of crashing or returning 500.
    """
    # Register CPE first
    await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-mqtt-test",
        "serial_number": "SN-MQTT-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })

    # Mock connection refused
    with patch.object(mqtt_publisher, "publish", side_effect=ConnectionRefusedError("Connection refused by broker")):
        r = await client.post("/api/v1/cpes/cpe-mqtt-test/reboot")
        assert r.status_code == 503
        assert "MQTT broker delivery failed" in r.json()["detail"]

    # Mock timeout error
    with patch.object(mqtt_publisher, "publish", side_effect=TimeoutError("Timed out publishing to broker")):
        r = await client.post("/api/v1/cpes/cpe-mqtt-test/reboot")
        assert r.status_code == 503
        assert "MQTT broker delivery failed" in r.json()["detail"]


# =============================================================================
# 4. CASCADING DELETION: LIVE STATE AND HISTORICAL METRICS
# =============================================================================

@pytest.mark.asyncio
async def test_cascading_deletion_with_heavy_history(client: AsyncClient):
    """
    Stress-tests cascading deletion:
    Registers a CPE, populates live state and 50 historical snapshots,
    then deletes the CPE and verifies all 51 dependent rows are wiped out.
    """
    cpe_id = "cpe-heavy-history"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-HEAVY-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })

    # Insert live state and 50 historical records
    async with TestingSessionLocal() as session:
        live = CpeLiveState(
            cpe_id=cpe_id,
            status="online",
            telemetry_metrics={"cpu_usage": 45.0, "optical_power": -19.5},
        )
        session.add(live)

        for i in range(50):
            hist = CpeHistoricalMetrics(
                cpe_id=cpe_id,
                status="online",
                telemetry_metrics={"cpu_usage": float(i), "rx_optical_power": -18.0 - (i * 0.1)},
                optical_power=Decimal(str(-18.0 - (i * 0.1))),
                change_reason="optical_signal_variation",
            )
            session.add(hist)
        await session.commit()

    # Verify history count before deletion
    r_hist_before = await client.get(f"/api/v1/cpes/{cpe_id}/history?limit=100")
    assert r_hist_before.status_code == 200
    assert r_hist_before.json()["total"] == 50

    # Delete CPE via API
    r_del = await client.delete(f"/api/v1/cpes/{cpe_id}")
    assert r_del.status_code == 200
    assert r_del.json() == {"status": "deleted", "cpe_id": cpe_id}

    # Verify inventory is 404
    r_get_cpe = await client.get(f"/api/v1/cpes/{cpe_id}")
    assert r_get_cpe.status_code == 404

    # Verify live-state is 404
    r_get_live = await client.get(f"/api/v1/cpes/{cpe_id}/live-state")
    assert r_get_live.status_code == 404

    # Verify history is 404
    r_get_hist = await client.get(f"/api/v1/cpes/{cpe_id}/history")
    assert r_get_hist.status_code == 404

    # Direct database verification
    async with TestingSessionLocal() as session:
        lives = (await session.execute(select(CpeLiveState).where(CpeLiveState.cpe_id == cpe_id))).scalars().all()
        hists = (await session.execute(select(CpeHistoricalMetrics).where(CpeHistoricalMetrics.cpe_id == cpe_id))).scalars().all()
        assert len(lives) == 0, f"Expected 0 live rows remaining, found {len(lives)}"
        assert len(hists) == 0, f"Expected 0 history rows remaining, found {len(hists)}"


# =============================================================================
# 5. MQTT REBOOT PAYLOAD FORMAT & REGEX COMPLIANCE
# =============================================================================

@pytest.mark.asyncio
async def test_mqtt_reboot_payload_matches_simulate_flow_regex(client: AsyncClient):
    """
    Validates that the command published to MQTT by POST /reboot:
    1. Topic is usp/endpoint/{cpe_id}/request
    2. QoS is 1
    3. Payload matches simulate_flow.sh regex: 'reboot|operate' (case insensitive)
    4. Valid JSON conforming to TR-369 USP Operate specifications
    """
    cpe_id = "cpe-regex-001"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-REGEX-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })

    published_topic = None
    published_payload = None
    published_qos = None

    async def mock_publish(topic: str, payload: str | bytes, qos: int = 1):
        nonlocal published_topic, published_payload, published_qos
        published_topic = topic
        published_payload = payload if isinstance(payload, str) else payload.decode("utf-8")
        published_qos = qos

    with patch.object(mqtt_publisher, "publish", side_effect=mock_publish):
        r = await client.post(f"/api/v1/cpes/{cpe_id}/reboot")
        assert r.status_code in (200, 202)

    # 1. Topic
    assert published_topic == f"usp/endpoint/{cpe_id}/request"
    # 2. QoS
    assert published_qos == 1
    # 3. Regex match
    regex_pattern = re.compile(r"reboot|operate", re.IGNORECASE)
    assert regex_pattern.search(published_payload) is not None, (
        f"Payload did not match regex 'reboot|operate': {published_payload}"
    )

    # 4. JSON structure
    data = json.loads(published_payload)
    assert data["cpe_id"] == cpe_id
    assert "reboot" in data["command"].lower()
    assert "operate" in data or "usp" in data
    assert data["usp"]["header"]["msg_type"] == "OPERATE"
    assert data["usp"]["body"]["request"]["operate"]["command"] == "Device.Reboot()"


# =============================================================================
# 6. PAGINATION & BOUNDARY ADVERSARIAL CASES
# =============================================================================

@pytest.mark.asyncio
async def test_pagination_boundaries(client: AsyncClient):
    """
    Checks negative skip, zero limit, limit > 1000.
    """
    # Negative skip -> 422
    r_neg_skip = await client.get("/api/v1/cpes?skip=-1")
    assert r_neg_skip.status_code == 422

    # Zero limit -> 422 (ge=1)
    r_zero_limit = await client.get("/api/v1/cpes?limit=0")
    assert r_zero_limit.status_code == 422

    # Limit > 1000 -> 422 (le=1000)
    r_over_limit = await client.get("/api/v1/cpes?limit=1001")
    assert r_over_limit.status_code == 422

    # History negative offset
    await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-page-cpe",
        "serial_number": "SN-PAGE-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })
    r_hist_neg = await client.get("/api/v1/cpes/cpe-page-cpe/history?offset=-1")
    assert r_hist_neg.status_code == 422


# =============================================================================
# 7. SCHEMA VALIDATION: OUI MAX_LENGTH CONSTRAINTS
# =============================================================================

@pytest.mark.asyncio
async def test_cpe_update_oui_max_length_validation(client: AsyncClient):
    """
    Verifies that CpeUpdate enforces max_length=6 on oui:
    - PUT/PATCH with valid oui (len <= 6) -> 200
    - PUT/PATCH with invalid oui (len > 6) -> 422
    """
    cpe_id = "cpe-oui-test"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-OUI-001",
        "manufacturer": "TP-Link",
        "model": "Archer-AX50",
    })

    # Valid OUI (len 6)
    r_valid = await client.put(f"/api/v1/cpes/{cpe_id}", json={"oui": "00259E"})
    assert r_valid.status_code == 200
    assert r_valid.json()["oui"] == "00259E"

    # Valid OUI (len 3)
    r_valid_short = await client.patch(f"/api/v1/cpes/{cpe_id}", json={"oui": "ABC"})
    assert r_valid_short.status_code == 200
    assert r_valid_short.json()["oui"] == "ABC"

    # Invalid OUI (len 7 > max_length=6) -> 422 Unprocessable Entity
    r_invalid_put = await client.put(f"/api/v1/cpes/{cpe_id}", json={"oui": "00259EA"})
    assert r_invalid_put.status_code == 422

    # Invalid OUI via PATCH (len 7 > max_length=6) -> 422 Unprocessable Entity
    r_invalid_patch = await client.patch(f"/api/v1/cpes/{cpe_id}", json={"oui": "1234567"})
    assert r_invalid_patch.status_code == 422

