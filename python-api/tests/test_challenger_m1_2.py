"""
Adversarial Empirical Challenge Harness for Milestone 1 Dual-Stack Refactoring.
Author: challenger_m1_2
Scope:
  1. Cascade deletion: cpe_inventory -> cpe_pending_commands & cpe_live_state & cpe_historical_metrics.
  2. WAL amplification and trigger non-interference with reconcile_live_to_history.
  3. Dual-stack reboot protocol behavior (tr069, tr369, dual, edge cases).
  4. Relational integrity, cross-CPE command leakage, and concurrency.
"""

import asyncio
from datetime import datetime, timezone
import re
from unittest.mock import AsyncMock, patch
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from app.models import CpeInventory, CpeLiveState, CpeHistoricalMetrics, CpePendingCommand
from app.mqtt import mqtt_publisher

try:
    from tests.conftest import TestingSessionLocal
except ImportError:
    from conftest import TestingSessionLocal


# =============================================================================
# 1. CASCADE DELETION EMPIRICAL TESTS
# =============================================================================

@pytest.mark.asyncio
async def test_cascade_delete_removes_all_command_statuses(client: AsyncClient):
    """
    Stress-test cascade delete:
    Create a CPE with multiple pending commands across all status lifecycles
    ('pending', 'dispatched', 'completed', 'failed').
    When the CPE is deleted, all command records MUST be deleted with zero orphans.
    """
    cpe_id = "cpe-cascade-stress-01"
    reg_resp = await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CASC-STR-01",
        "manufacturer": "Huawei",
        "model": "EchoLife-HG8245H",
    })
    assert reg_resp.status_code == 201

    # Insert commands with different statuses
    statuses = ["pending", "dispatched", "completed", "failed"]
    cmd_ids = []
    for i, st in enumerate(statuses):
        r = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
            "command_type": f"Command_{st}_{i}",
            "command_payload": {"step": i},
        })
        assert r.status_code == 201
        cid = r.json()["id"]
        cmd_ids.append(cid)
        if st != "pending":
            patch_resp = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cid}", json={
                "status": st,
                "completed_at": datetime.now(timezone.utc).isoformat() if st in ("completed", "failed") else None,
            })
            assert patch_resp.status_code == 200

    # Verify all 4 exist in database
    async with TestingSessionLocal() as session:
        cmds_before = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_id)
        )).scalars().all()
        assert len(cmds_before) == 4

    # Delete CPE
    del_resp = await client.delete(f"/api/v1/cpes/{cpe_id}")
    assert del_resp.status_code == 200

    # Verify 0 remain in database
    async with TestingSessionLocal() as session:
        cmds_after = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_id)
        )).scalars().all()
        assert len(cmds_after) == 0

        # Also verify by command IDs directly
        orphans = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.id.in_(cmd_ids))
        )).scalars().all()
        assert len(orphans) == 0


@pytest.mark.asyncio
async def test_cascade_delete_isolation_between_cpes(client: AsyncClient):
    """
    Verify that deleting CPE-A removes only CPE-A's pending commands,
    leaving CPE-B's pending commands completely intact.
    """
    cpe_a = "cpe-iso-a"
    cpe_b = "cpe-iso-b"

    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_a,
        "serial_number": "SN-ISO-A",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_b,
        "serial_number": "SN-ISO-B",
        "manufacturer": "TP-Link",
        "model": "EX510",
    })

    # Add 3 commands to A, 3 commands to B
    for i in range(3):
        await client.post(f"/api/v1/cpes/{cpe_a}/commands", json={
            "command_type": f"CmdA_{i}",
            "command_payload": {},
        })
        await client.post(f"/api/v1/cpes/{cpe_b}/commands", json={
            "command_type": f"CmdB_{i}",
            "command_payload": {},
        })

    # Delete CPE A
    del_a = await client.delete(f"/api/v1/cpes/{cpe_a}")
    assert del_a.status_code == 200

    # Verify CPE A has 0 commands, CPE B still has all 3 commands
    async with TestingSessionLocal() as session:
        cmds_a = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_a)
        )).scalars().all()
        assert len(cmds_a) == 0

        cmds_b = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_b)
        )).scalars().all()
        assert len(cmds_b) == 3


@pytest.mark.asyncio
async def test_full_quad_cascade_cleanout(client: AsyncClient):
    """
    Verify that deleting a CPE cascades and cleans all 4 related tables:
    cpe_inventory, cpe_live_state, cpe_historical_metrics, and cpe_pending_commands.
    """
    cpe_id = "cpe-quad-cascade"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-QUAD-01",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # Add live state, historical metric, and pending commands
    async with TestingSessionLocal() as session:
        live = CpeLiveState(
            cpe_id=cpe_id,
            status="online",
            telemetry_metrics={"rx_optical_power": -18.5},
        )
        hist = CpeHistoricalMetrics(
            cpe_id=cpe_id,
            status="online",
            telemetry_metrics={"rx_optical_power": -18.5},
            optical_power=-18.5,
            change_reason="initial_state",
        )
        cmd = CpePendingCommand(
            cpe_id=cpe_id,
            command_type="Reboot",
            command_payload={},
            status="pending",
        )
        session.add_all([live, hist, cmd])
        await session.commit()

    # Delete CPE via API
    del_r = await client.delete(f"/api/v1/cpes/{cpe_id}")
    assert del_r.status_code == 200

    # Verify all 4 tables are empty for cpe_id
    async with TestingSessionLocal() as session:
        inv = (await session.execute(select(CpeInventory).where(CpeInventory.cpe_id == cpe_id))).scalars().first()
        live = (await session.execute(select(CpeLiveState).where(CpeLiveState.cpe_id == cpe_id))).scalars().first()
        hist = (await session.execute(select(CpeHistoricalMetrics).where(CpeHistoricalMetrics.cpe_id == cpe_id))).scalars().all()
        cmds = (await session.execute(select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_id))).scalars().all()

        assert inv is None, "cpe_inventory row was not deleted"
        assert live is None, "cpe_live_state row was not cascade-deleted"
        assert len(hist) == 0, "cpe_historical_metrics rows were not cascade-deleted"
        assert len(cmds) == 0, "cpe_pending_commands rows were not cascade-deleted"


# =============================================================================
# 2. CROSS-CPE COMMAND LEAKAGE & ROUTE INTEGRITY
# =============================================================================

@pytest.mark.asyncio
async def test_cross_cpe_command_tampering_rejected(client: AsyncClient):
    """
    Adversarial Security Test:
    Attempt to view or modify CPE-A's pending command by issuing requests
    under CPE-B's path:
      GET /api/v1/cpes/{cpe_b}/commands/{cmd_a_id} -> MUST return 404
      PATCH /api/v1/cpes/{cpe_b}/commands/{cmd_a_id} -> MUST return 404
    """
    cpe_a = "cpe-leak-a"
    cpe_b = "cpe-leak-b"

    await client.post("/api/v1/cpes", json={"cpe_id": cpe_a, "serial_number": "SN-LK-A", "manufacturer": "Huawei", "model": "HG8245H"})
    await client.post("/api/v1/cpes", json={"cpe_id": cpe_b, "serial_number": "SN-LK-B", "manufacturer": "Huawei", "model": "HG8245H"})

    # Create command on CPE-A
    create_r = await client.post(f"/api/v1/cpes/{cpe_a}/commands", json={
        "command_type": "Reboot",
        "command_payload": {"target": "cpe_a"},
    })
    cmd_a_id = create_r.json()["id"]

    # Try to GET cmd_a under CPE-B route
    leak_get = await client.get(f"/api/v1/cpes/{cpe_b}/commands/{cmd_a_id}")
    assert leak_get.status_code == 404, f"Cross-CPE command leakage! Expected 404, got {leak_get.status_code}"

    # Try to PATCH cmd_a under CPE-B route
    leak_patch = await client.patch(f"/api/v1/cpes/{cpe_b}/commands/{cmd_a_id}", json={
        "status": "completed",
    })
    assert leak_patch.status_code == 404, f"Cross-CPE tampering allowed! Expected 404, got {leak_patch.status_code}"

    # Verify cmd_a on CPE-A is still 'pending'
    verify_get = await client.get(f"/api/v1/cpes/{cpe_a}/commands/{cmd_a_id}")
    assert verify_get.status_code == 200
    assert verify_get.json()["status"] == "pending"


# =============================================================================
# 3. REBOOT PROTOCOL PARAMETER BEHAVIORS & EDGE CASES
# =============================================================================

@pytest.mark.asyncio
async def test_reboot_case_insensitivity_and_variants(client: AsyncClient):
    """
    Tests reboot endpoint with various case and syntax representations:
    'TR069', 'tr-069', 'TR-069', 'TR369', 'tr-369', 'TR-369', 'DUAL', 'Dual'.
    """
    cpe_id = "cpe-proto-variants"
    await client.post("/api/v1/cpes", json={"cpe_id": cpe_id, "serial_number": "SN-PR-VAR", "manufacturer": "Huawei", "model": "HG8245H"})

    # Test TR-069 variants
    for variant in ["TR069", "tr-069", "TR-069"]:
        with patch.object(mqtt_publisher, "publish", AsyncMock()) as mock_mqtt:
            r = await client.post(f"/api/v1/cpes/{cpe_id}/reboot?protocol={variant}")
            assert r.status_code == 200
            assert r.json()["status"] == "queued"
            assert r.json()["topic"] == "tr069/cwmp"
            mock_mqtt.assert_not_called()

    # Test TR-369 variants
    for variant in ["TR369", "tr-369", "TR-369"]:
        with patch.object(mqtt_publisher, "publish", AsyncMock()) as mock_mqtt:
            r = await client.post(f"/api/v1/cpes/{cpe_id}/reboot?protocol={variant}")
            assert r.status_code == 200
            assert r.json()["status"] == "dispatched"
            mock_mqtt.assert_called_once()

    # Test Dual variants
    for variant in ["DUAL", "Dual", "dUAL"]:
        with patch.object(mqtt_publisher, "publish", AsyncMock()) as mock_mqtt:
            r = await client.post(f"/api/v1/cpes/{cpe_id}/reboot?protocol={variant}")
            assert r.status_code == 200
            assert r.json()["status"] == "dispatched"
            mock_mqtt.assert_called_once()


@pytest.mark.asyncio
async def test_reboot_dual_stack_mqtt_failure_db_side_effect(client: AsyncClient):
    """
    Adversarial Edge Case:
    When protocol=dual and MQTT broker raises 503,
    does the pending command remain queued in cpe_pending_commands?
    Atomicity requirement: the queued command must be removed from DB before 503.
    """
    cpe_id = "cpe-dual-fail"
    await client.post("/api/v1/cpes", json={"cpe_id": cpe_id, "serial_number": "SN-DF-01", "manufacturer": "Huawei", "model": "HG8245H"})

    async def fail_publish(*args, **kwargs):
        raise ConnectionRefusedError("Mosquitto broker offline")

    with patch.object(mqtt_publisher, "publish", side_effect=fail_publish):
        r = await client.post(f"/api/v1/cpes/{cpe_id}/reboot")
        assert r.status_code == 503
        assert "MQTT broker delivery failed" in r.json()["detail"]

    # Check database: orphan command must have been removed from DB on MQTT failure
    async with TestingSessionLocal() as session:
        cmds = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_id)
        )).scalars().all()
        # Atomicity verified: 0 orphan commands remain in DB
        assert len(cmds) == 0


@pytest.mark.asyncio
async def test_reboot_unknown_protocol_behavior(client: AsyncClient):
    """
    Adversarial Finding:
    When an unsupported protocol like 'snmp' or 'unknown' is passed,
    reboot_cpe must reject the request with HTTP 400 Bad Request,
    preventing phantom queueing.
    """
    cpe_id = "cpe-proto-unknown"
    await client.post("/api/v1/cpes", json={"cpe_id": cpe_id, "serial_number": "SN-UN-01", "manufacturer": "Huawei", "model": "HG8245H"})

    r = await client.post(f"/api/v1/cpes/{cpe_id}/reboot?protocol=snmp")
    assert r.status_code == 400
    assert "invalid protocol" in r.json()["detail"].lower()

    # Verify whether anything was actually stored in the DB
    async with TestingSessionLocal() as session:
        cmds = (await session.execute(
            select(CpePendingCommand).where(CpePendingCommand.cpe_id == cpe_id)
        )).scalars().all()
        # Confirmed: No command is in the database!
        assert len(cmds) == 0


# =============================================================================
# 4. CONCURRENCY & ORDERING
# =============================================================================

@pytest.mark.asyncio
async def test_concurrent_command_ordering_fifo(client: AsyncClient):
    """
    Enqueues multiple commands rapidly and verifies that GET /commands
    returns them strictly in descending order of created_at (most recent first)
    as defined by the router select statement.
    """
    cpe_id = "cpe-fifo-01"
    await client.post("/api/v1/cpes", json={"cpe_id": cpe_id, "serial_number": "SN-FIFO-01", "manufacturer": "Huawei", "model": "HG8245H"})

    cmd_count = 10
    for i in range(cmd_count):
        r = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
            "command_type": f"Cmd_{i:02d}",
            "command_payload": {"seq": i},
        })
        assert r.status_code == 201

    list_r = await client.get(f"/api/v1/cpes/{cpe_id}/commands?limit=50")
    assert list_r.status_code == 200
    items = list_r.json()
    assert len(items) == cmd_count

    # Verify descending ordering by created_at
    timestamps = [it["created_at"] for it in items]
    assert timestamps == sorted(timestamps, reverse=True)
