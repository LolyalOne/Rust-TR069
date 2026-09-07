"""CPE endpoints router for inventory CRUD, live state, history, and command dispatch."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import (
    CpeHistoricalMetrics,
    CpeInventory,
    CpeLiveState,
    CpePendingCommand,
)
from app.mqtt import mqtt_publisher
from app.schemas import (
    CommandDispatchResponse,
    CpeCreate,
    CpeHistoryItem,
    CpeHistoryResponse,
    CpeLiveStateResponse,
    CpeResponse,
    CpeUpdate,
    PendingCommandCreate,
    PendingCommandResponse,
    PendingCommandStatus,
    PendingCommandUpdate,
)

router = APIRouter(prefix="/cpes", tags=["CPEs"])


@router.post("", response_model=CpeResponse, status_code=status.HTTP_201_CREATED)
async def create_cpe(
    payload: CpeCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Registers a new CPE in cpe_inventory.
    Handles upsert/re-registration if device was auto-discovered by Rust Core.
    """
    existing = await db.get(CpeInventory, payload.cpe_id)
    if existing:
        # Update existing record with authoritative registration data
        existing.serial_number = payload.serial_number
        existing.manufacturer = payload.manufacturer
        existing.model = payload.model
        existing.oui = payload.oui
        existing.product_class = payload.product_class
        existing.hardware_version = payload.hardware_version
        existing.software_version = payload.software_version
        existing.description = payload.description
        try:
            await db.commit()
            await db.refresh(existing)
            response.status_code = status.HTTP_200_OK
            return existing
        except IntegrityError as err:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"CPE with serial_number '{payload.serial_number}' already exists: {err}",
            )

    new_cpe = CpeInventory(**payload.model_dump())
    db.add(new_cpe)
    try:
        await db.commit()
        await db.refresh(new_cpe)
        return new_cpe
    except IntegrityError as err:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"CPE with serial_number '{payload.serial_number}' already exists: {err}",
        )


@router.get("", response_model=list[CpeResponse])
async def list_cpes(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
):
    """Lists registered CPEs with optional status filter and pagination."""
    stmt = select(CpeInventory).offset(skip).limit(limit).order_by(CpeInventory.created_at.desc())
    if status_filter:
        stmt = stmt.where(CpeInventory.status == status_filter)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{cpe_id}", response_model=CpeResponse)
async def get_cpe(cpe_id: str, db: AsyncSession = Depends(get_db)):
    """Retrieves authoritative details of a specific CPE."""
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")
    return cpe


@router.put("/{cpe_id}", response_model=CpeResponse)
@router.patch("/{cpe_id}", response_model=CpeResponse)
async def update_cpe(
    cpe_id: str,
    payload: CpeUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Updates editable inventory fields of a registered CPE."""
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(cpe, field, value)

    await db.commit()
    await db.refresh(cpe)
    return cpe


@router.delete("/{cpe_id}", status_code=status.HTTP_200_OK)
async def delete_cpe(cpe_id: str, db: AsyncSession = Depends(get_db)):
    """Deletes a CPE and cascades deletion to live state and history."""
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    await db.delete(cpe)
    await db.commit()
    return {"status": "deleted", "cpe_id": cpe_id}


@router.get("/{cpe_id}/live-state", response_model=CpeLiveStateResponse)
@router.get("/{cpe_id}/live", response_model=CpeLiveStateResponse)
async def get_cpe_live_state(cpe_id: str, db: AsyncSession = Depends(get_db)):
    """
    Queries real-time telemetry and parameters directly from in-RAM cpe_live_state table.
    Provides aliases metrics and parameters for test harness compatibility.
    """
    live = await db.get(CpeLiveState, cpe_id)
    if not live:
        # Check if inventory exists
        inv = await db.get(CpeInventory, cpe_id)
        if not inv:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Live state not available yet for CPE '{cpe_id}'",
        )

    telemetry = live.telemetry_metrics or {}
    params = live.current_parameters or {}

    return CpeLiveStateResponse(
        cpe_id=live.cpe_id,
        endpoint_id=live.endpoint_id,
        status=live.status,
        telemetry_metrics=telemetry,
        metrics=telemetry,  # Alias
        current_parameters=params,
        parameters=params,  # Alias
        ip_address=live.ip_address,
        firmware_version=live.firmware_version,
        last_seen=live.last_seen,
        updated_at=live.updated_at,
    )


@router.get("/{cpe_id}/history", response_model=CpeHistoryResponse)
async def get_cpe_history(
    cpe_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Queries persistent historical snapshots populated by reconciliation trigger."""
    inv = await db.get(CpeInventory, cpe_id)
    if not inv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    stmt = (
        select(CpeHistoricalMetrics)
        .where(CpeHistoricalMetrics.cpe_id == cpe_id)
        .order_by(desc(CpeHistoricalMetrics.recorded_at), desc(CpeHistoricalMetrics.id))
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    records = result.scalars().all()

    items = [
        CpeHistoryItem(
            id=r.id,
            cpe_id=r.cpe_id,
            status=r.status,
            current_parameters=r.current_parameters,
            telemetry_metrics=r.telemetry_metrics or {},
            optical_power=float(r.optical_power) if r.optical_power is not None else None,
            recorded_at=r.recorded_at,
            change_reason=r.change_reason,
        )
        for r in records
    ]

    return CpeHistoryResponse(cpe_id=cpe_id, total=len(items), items=items)


@router.post("/{cpe_id}/reboot", response_model=CommandDispatchResponse, status_code=status.HTTP_200_OK)
async def reboot_cpe(
    cpe_id: str,
    protocol: Optional[str] = Query(None, description="tr069, tr369, or dual"),
    db: AsyncSession = Depends(get_db),
):
    """
    Dispatches Reboot command:
      - For TR-369 (USP): Publishes operate request to Mosquitto MQTT broker.
      - For TR-069 (CWMP): Queues command in cpe_pending_commands table.
      - Default ('dual' or omitted): Queues in cpe_pending_commands AND publishes to MQTT.
    """
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    raw_proto = protocol or "dual"
    proto = raw_proto.lower().replace("-", "")
    if proto not in ("tr069", "tr369", "dual"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid protocol '{protocol}'. Allowed: 'tr069', 'tr369', 'dual'",
        )

    # If TR-069 or dual, enqueue in cpe_pending_commands
    pending_cmd = None
    if proto in ("tr069", "dual"):
        pending_cmd = CpePendingCommand(
            cpe_id=cpe_id,
            command_type="Reboot",
            command_payload={"command": "Reboot", "command_key": f"reboot-{cpe_id}"},
            status="pending",
        )
        db.add(pending_cmd)
        await db.commit()
        await db.refresh(pending_cmd)

    # If TR-369 or dual, publish to MQTT
    if proto in ("tr369", "dual"):
        try:
            dispatch_info = await mqtt_publisher.publish_reboot_command(cpe_id)
        except Exception as e:
            if pending_cmd is not None:
                await db.delete(pending_cmd)
                await db.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"MQTT broker delivery failed: {e}",
            )

        return CommandDispatchResponse(
            status="dispatched",
            cpe_id=dispatch_info["cpe_id"],
            command=dispatch_info["command"],
            command_key=dispatch_info["command_key"],
            topic=dispatch_info["topic"],
            dispatched_at=dispatch_info["dispatched_at"],
        )
    else:
        # TR-069 only mode
        now = datetime.now(timezone.utc)
        return CommandDispatchResponse(
            status="queued",
            cpe_id=cpe_id,
            command="Reboot",
            command_key=str(pending_cmd.id) if pending_cmd else f"reboot-{cpe_id}",
            topic="tr069/cwmp",
            dispatched_at=now,
        )


@router.post(
    "/{cpe_id}/commands",
    response_model=PendingCommandResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enqueue_cpe_command(
    cpe_id: str,
    payload: PendingCommandCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Enqueues a pending command (e.g. Reboot, GetParameterValues) in cpe_pending_commands.
    Awaiting dispatch during the next TR-069 Inform polling session.
    """
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    cmd = CpePendingCommand(
        cpe_id=cpe_id,
        command_type=payload.command_type,
        command_payload=payload.command_payload,
        status="pending",
    )
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)
    return cmd


@router.get(
    "/{cpe_id}/commands",
    response_model=list[PendingCommandResponse],
    status_code=status.HTTP_200_OK,
)
async def list_cpe_commands(
    cpe_id: str,
    status_filter: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """
    Lists pending and historical commands for a specific CPE device.
    Supports filtering by status (e.g. pending, dispatched, completed, failed) and pagination.
    """
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    stmt = (
        select(CpePendingCommand)
        .where(CpePendingCommand.cpe_id == cpe_id)
        .order_by(desc(CpePendingCommand.created_at))
        .offset(skip)
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(CpePendingCommand.status == status_filter)

    result = await db.execute(stmt)
    return result.scalars().all()


@router.get(
    "/{cpe_id}/commands/{command_id}",
    response_model=PendingCommandResponse,
    status_code=status.HTTP_200_OK,
)
async def get_cpe_command(
    cpe_id: str,
    command_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Retrieves a specific command by its ID for a CPE device."""
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    stmt = select(CpePendingCommand).where(
        CpePendingCommand.cpe_id == cpe_id,
        CpePendingCommand.id == str(command_id),
    )
    result = await db.execute(stmt)
    cmd = result.scalar_one_or_none()
    if not cmd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Command '{command_id}' not found")
    return cmd


@router.patch(
    "/{cpe_id}/commands/{command_id}",
    response_model=PendingCommandResponse,
    status_code=status.HTTP_200_OK,
)
async def update_cpe_command(
    cpe_id: str,
    command_id: UUID,
    payload: PendingCommandUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Updates status, timestamps, or result payload of a pending command."""
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    stmt = select(CpePendingCommand).where(
        CpePendingCommand.cpe_id == cpe_id,
        CpePendingCommand.id == str(command_id),
    )
    result = await db.execute(stmt)
    cmd = result.scalar_one_or_none()
    if not cmd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Command '{command_id}' not found")

    update_data = payload.model_dump(exclude_unset=True)

    if "status" in update_data and update_data["status"] is not None:
        new_status = (
            update_data["status"].value
            if isinstance(update_data["status"], Enum)
            else str(update_data["status"])
        )
        if cmd.status in ("completed", "failed") and new_status in ("pending", "dispatched"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot transition command from terminal status '{cmd.status}' to '{new_status}'",
            )
        if cmd.status in ("completed", "failed") and new_status != cmd.status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot transition command from terminal status '{cmd.status}' to '{new_status}'",
            )
        update_data["status"] = new_status

    for k, v in update_data.items():
        setattr(cmd, k, v)

    await db.commit()
    await db.refresh(cmd)
    return cmd

