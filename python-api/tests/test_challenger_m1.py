"""
Empirical Adversarial Challenge Test Suite for Milestone 1 Dual-Stack Refactoring.
Probes edge cases, malformed payloads, oversized JSON, invalid UUIDs,
SQL injection vectors, status state transitions, and concurrent execution.
"""

import asyncio
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from app.models import CpeInventory, CpePendingCommand
from tests.conftest import TestingSessionLocal


# =============================================================================
# 1. MALFORMED PAYLOADS & BOUNDARY VALIDATION
# =============================================================================

@pytest.mark.asyncio
async def test_command_payload_type_validation(client: AsyncClient):
    """Tests that non-dict command_payload values are rejected with 422."""
    await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-challenger-001",
        "serial_number": "SN-CHAL-001",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # String instead of dict
    r_str = await client.post("/api/v1/cpes/cpe-challenger-001/commands", json={
        "command_type": "Reboot",
        "command_payload": "invalid_string",
    })
    assert r_str.status_code == 422, f"Expected 422 for string payload, got {r_str.status_code}"

    # List instead of dict
    r_list = await client.post("/api/v1/cpes/cpe-challenger-001/commands", json={
        "command_type": "Reboot",
        "command_payload": ["param1", "param2"],
    })
    assert r_list.status_code == 422, f"Expected 422 for list payload, got {r_list.status_code}"

    # Integer instead of dict
    r_int = await client.post("/api/v1/cpes/cpe-challenger-001/commands", json={
        "command_type": "Reboot",
        "command_payload": 12345,
    })
    assert r_int.status_code == 422, f"Expected 422 for integer payload, got {r_int.status_code}"


@pytest.mark.asyncio
async def test_command_type_boundaries(client: AsyncClient):
    """Tests boundaries on command_type: empty string, >64 chars, exact 64 chars."""
    await client.post("/api/v1/cpes", json={
        "cpe_id": "cpe-challenger-002",
        "serial_number": "SN-CHAL-002",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # Empty string -> 422
    r_empty = await client.post("/api/v1/cpes/cpe-challenger-002/commands", json={
        "command_type": "",
        "command_payload": {},
    })
    assert r_empty.status_code == 422

    # Exactly 64 chars -> 201 (Valid boundary)
    len_64 = "C" * 64
    r_64 = await client.post("/api/v1/cpes/cpe-challenger-002/commands", json={
        "command_type": len_64,
        "command_payload": {},
    })
    assert r_64.status_code == 201
    assert r_64.json()["command_type"] == len_64

    # 65 chars -> 422 (Exceeds max_length)
    len_65 = "C" * 65
    r_65 = await client.post("/api/v1/cpes/cpe-challenger-002/commands", json={
        "command_type": len_65,
        "command_payload": {},
    })
    assert r_65.status_code == 422


# =============================================================================
# 2. OVERSIZED JSON & DEEPLY NESTED PAYLOADS
# =============================================================================

@pytest.mark.asyncio
async def test_oversized_json_payloads(client: AsyncClient):
    """Tests persistence and retrieval of large (>64KB) and deeply nested payloads."""
    cpe_id = "cpe-challenger-003"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-003",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # 1. Payload > 64KB (approx 70KB with 1000 key-value pairs)
    large_payload = {f"param_{i:04d}": "value_" * 10 for i in range(1000)}
    r_large = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": "SetParameterValues",
        "command_payload": large_payload,
    })
    assert r_large.status_code == 201
    cmd_id = r_large.json()["id"]

    # Fetch back and verify exact payload integrity
    r_get = await client.get(f"/api/v1/cpes/{cpe_id}/commands/{cmd_id}")
    assert r_get.status_code == 200
    assert len(r_get.json()["command_payload"]) == 1000

    # 2. Deeply nested JSON (35 levels)
    nested = {"leaf": "deep_value"}
    for level in range(35):
        nested = {f"level_{level}": nested}

    r_nested = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": "SetParameterValues",
        "command_payload": nested,
    })
    assert r_nested.status_code == 201
    cmd_nested_id = r_nested.json()["id"]

    r_nested_get = await client.get(f"/api/v1/cpes/{cpe_id}/commands/{cmd_nested_id}")
    assert r_nested_get.status_code == 200
    assert "level_34" in r_nested_get.json()["command_payload"]

    # 3. XML / SOAP string inside payload
    soap_body = '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"><cwmp:Reboot><CommandKey>KEY123</CommandKey></cwmp:Reboot></SOAP-ENV:Envelope>'
    r_soap = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": "Reboot",
        "command_payload": {"soap_raw": soap_body, "special_chars": "<>&\"'\n\r\té中文"},
    })
    assert r_soap.status_code == 201
    assert r_soap.json()["command_payload"]["soap_raw"] == soap_body


# =============================================================================
# 3. INVALID UUIDs & PATH PARAMETERS
# =============================================================================

@pytest.mark.asyncio
async def test_invalid_uuid_handling_in_endpoints(client: AsyncClient):
    """Verifies behavior when command_id path parameter is non-UUID, malformed, or SQLi."""
    cpe_id = "cpe-challenger-004"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-004",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    invalid_uuids = [
        "not-a-uuid",
        "12345",
        "00000000-0000-0000-0000-00000000000g",  # invalid hex
        "' OR '1'='1",
        "../etc/passwd",
        "undefined",
        "null",
    ]

    for bad_id in invalid_uuids:
        # GET should return 422 via FastAPI UUID typing (or 404 for path-normalization escaping the route), never 500
        expected_status = 404 if ".." in bad_id else 422
        r_get = await client.get(f"/api/v1/cpes/{cpe_id}/commands/{bad_id}")
        assert r_get.status_code == expected_status, f"Expected {expected_status} for command_id='{bad_id}', got {r_get.status_code}"

        # PATCH should return 422 via FastAPI UUID typing (or 404 for path-normalization), never 500
        r_patch = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{bad_id}", json={
            "status": "completed",
        })
        assert r_patch.status_code == expected_status, f"Expected {expected_status} on PATCH for command_id='{bad_id}', got {r_patch.status_code}"

    # Valid UUID format but nonexistent in DB -> should cleanly return 404
    nonexistent_uuid = str(uuid.uuid4())
    r_nonexistent = await client.get(f"/api/v1/cpes/{cpe_id}/commands/{nonexistent_uuid}")
    assert r_nonexistent.status_code == 404


# =============================================================================
# 4. SQL INJECTION PROBING
# =============================================================================

@pytest.mark.asyncio
async def test_sqli_resilience_on_pending_command_endpoints(client: AsyncClient):
    """Attempts SQL injection across cpe_id, command_type, status query, and pagination."""
    cpe_id = "cpe-challenger-005"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-005",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # 1. SQLi in command_type
    sqli_cmd_type = "'; DROP TABLE cpe_pending_commands; --"
    r_sqli_create = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": sqli_cmd_type,
        "command_payload": {"target": "db"},
    })
    assert r_sqli_create.status_code == 201
    cmd_id = r_sqli_create.json()["id"]

    # Verify table was NOT dropped and record is preserved as literal
    r_sqli_get = await client.get(f"/api/v1/cpes/{cpe_id}/commands/{cmd_id}")
    assert r_sqli_get.status_code == 200
    assert r_sqli_get.json()["command_type"] == sqli_cmd_type

    # 2. SQLi in status filter query param
    sqli_status_filters = [
        "' OR '1'='1",
        "pending' OR '1'='1",
        "pending'; DROP TABLE cpe_inventory; --",
        "UNION SELECT * FROM cpe_pending_commands --",
    ]
    for sqli_status in sqli_status_filters:
        r_list = await client.get(f"/api/v1/cpes/{cpe_id}/commands?status={sqli_status}")
        assert r_list.status_code == 200
        # Should return empty list because no status matches literal SQLi string
        assert r_list.json() == []

    # 3. SQLi in cpe_id path
    sqli_cpe_paths = [
        "' OR '1'='1",
        "cpe-005' OR '1'='1",
        "'; DELETE FROM cpe_pending_commands; --",
    ]
    for bad_cpe in sqli_cpe_paths:
        r_bad_cpe = await client.get(f"/api/v1/cpes/{bad_cpe}/commands")
        assert r_bad_cpe.status_code == 404

    # 4. Verify all tables and data remain fully intact in DB
    async with TestingSessionLocal() as session:
        all_cmds = (await session.execute(select(CpePendingCommand))).scalars().all()
        assert len(all_cmds) >= 1


# =============================================================================
# 5. STATUS TRANSITIONS & LIFECYCLE BEHAVIOR
# =============================================================================

@pytest.mark.asyncio
async def test_status_max_length_and_values(client: AsyncClient):
    """Tests status length boundary and lifecycle updates."""
    cpe_id = "cpe-challenger-006"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-006",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    r = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": "Reboot",
        "command_payload": {},
    })
    cmd_id = r.json()["id"]

    # Status length > 32 chars -> 422
    r_long_status = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd_id}", json={
        "status": "A" * 33,
    })
    assert r_long_status.status_code == 422

    # Standard progression: pending -> dispatched -> completed
    r_disp = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd_id}", json={
        "status": "dispatched",
        "dispatched_at": "2026-09-07T14:00:00Z",
    })
    assert r_disp.status_code == 200
    assert r_disp.json()["status"] == "dispatched"

    r_comp = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd_id}", json={
        "status": "completed",
        "completed_at": "2026-09-07T14:01:00Z",
        "result_payload": {"reboot_status": "Success"},
    })
    assert r_comp.status_code == 200
    assert r_comp.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_state_machine_rewind_from_terminal_states_prohibited(client: AsyncClient):
    """
    Verifies that commands in terminal states ('completed', 'failed')
    cannot be rewound back to 'pending' or 'dispatched', returning HTTP 400.
    Also verifies that invalid status strings return HTTP 422.
    """
    cpe_id = "cpe-challenger-sm"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-SM",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # 1. Invalid status string -> 422 Unprocessable Entity
    r_create = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": "Reboot",
        "command_payload": {},
    })
    cmd1_id = r_create.json()["id"]

    r_invalid_status = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd1_id}", json={
        "status": "completely_bogus_status",
    })
    assert r_invalid_status.status_code == 422

    # 2. Advance cmd1 to 'completed'
    r_comp = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd1_id}", json={
        "status": "completed",
        "completed_at": "2026-09-07T14:01:00Z",
    })
    assert r_comp.status_code == 200

    # Attempt rewind completed -> pending -> 400 Bad Request
    r_rewind_pend = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd1_id}", json={
        "status": "pending",
    })
    assert r_rewind_pend.status_code == 400
    assert "terminal status" in r_rewind_pend.json()["detail"].lower()

    # Attempt rewind completed -> dispatched -> 400 Bad Request
    r_rewind_disp = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd1_id}", json={
        "status": "dispatched",
    })
    assert r_rewind_disp.status_code == 400
    assert "terminal status" in r_rewind_disp.json()["detail"].lower()

    # 3. Create cmd2 and advance to 'failed'
    r_create2 = await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
        "command_type": "Reboot",
        "command_payload": {},
    })
    cmd2_id = r_create2.json()["id"]
    r_fail = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd2_id}", json={
        "status": "failed",
    })
    assert r_fail.status_code == 200

    # Attempt rewind failed -> pending -> 400 Bad Request
    r_fail_pend = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd2_id}", json={
        "status": "pending",
    })
    assert r_fail_pend.status_code == 400

    # Attempt rewind failed -> dispatched -> 400 Bad Request
    r_fail_disp = await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{cmd2_id}", json={
        "status": "dispatched",
    })
    assert r_fail_disp.status_code == 400


@pytest.mark.asyncio
async def test_reboot_endpoint_phantom_queue_vulnerability(client: AsyncClient):
    """
    Empirically verifies that invalid protocol parameter is rejected with HTTP 400,
    preventing phantom queueing.
    """
    cpe_id = "cpe-challenger-007"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-007",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # Call reboot with an unsupported protocol string -> must return 400
    r_bogus = await client.post(f"/api/v1/cpes/{cpe_id}/reboot?protocol=invalid_protocol")
    assert r_bogus.status_code == 400
    assert "invalid protocol" in r_bogus.json()["detail"].lower()

    # Now verify cpe_pending_commands table: IT IS EMPTY!
    r_queue = await client.get(f"/api/v1/cpes/{cpe_id}/commands")
    assert r_queue.status_code == 200
    queued_cmds = r_queue.json()
    assert len(queued_cmds) == 0, "No commands should be queued on rejected protocol"


# =============================================================================
# 6. CONCURRENT ACCESS & STRESS
# =============================================================================

@pytest.mark.asyncio
async def test_concurrent_command_enqueuing_and_patching(client: AsyncClient):
    """Stress tests concurrent POST and PATCH requests without deadlock or loss."""
    cpe_id = "cpe-challenger-008"
    await client.post("/api/v1/cpes", json={
        "cpe_id": cpe_id,
        "serial_number": "SN-CHAL-008",
        "manufacturer": "Huawei",
        "model": "HG8245H",
    })

    # Concurrently enqueue 20 commands
    async def enqueue_worker(idx: int):
        return await client.post(f"/api/v1/cpes/{cpe_id}/commands", json={
            "command_type": f"Command_{idx:02d}",
            "command_payload": {"idx": idx},
        })

    tasks = [enqueue_worker(i) for i in range(20)]
    responses = await asyncio.gather(*tasks)

    for r in responses:
        assert r.status_code == 201

    # Verify all 20 have unique IDs
    cmd_ids = [r.json()["id"] for r in responses]
    assert len(set(cmd_ids)) == 20

    # Concurrently update all 20 commands to 'dispatched'
    async def patch_worker(c_id: str, idx: int):
        return await client.patch(f"/api/v1/cpes/{cpe_id}/commands/{c_id}", json={
            "status": "dispatched",
            "dispatched_at": f"2026-09-07T14:0{idx%10}:00Z",
        })

    patch_tasks = [patch_worker(c_id, i) for i, c_id in enumerate(cmd_ids)]
    patch_responses = await asyncio.gather(*patch_tasks)

    for pr in patch_responses:
        assert pr.status_code == 200
        assert pr.json()["status"] == "dispatched"

    # Verify list count
    r_list = await client.get(f"/api/v1/cpes/{cpe_id}/commands?status=dispatched&limit=50")
    assert r_list.status_code == 200
    assert len(r_list.json()) == 20
