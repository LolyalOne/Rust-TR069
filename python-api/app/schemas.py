"""Pydantic V2 schemas for TR-369 USP ACS Manager API."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


# -----------------------------------------------------------------------------
# CPE Inventory Schemas
# -----------------------------------------------------------------------------

class CpeBase(BaseModel):
    serial_number: str = Field(..., min_length=1, max_length=64, description="Serial number of CPE")
    manufacturer: str = Field(..., min_length=1, max_length=64, description="Manufacturer")
    model: str = Field(..., min_length=1, max_length=64, description="Device model")
    oui: Optional[str] = Field(None, max_length=6, description="Organizationally Unique Identifier")
    product_class: Optional[str] = Field(None, max_length=64, description="Product class")
    hardware_version: Optional[str] = Field(None, max_length=64, description="Hardware version")
    software_version: Optional[str] = Field(None, max_length=64, description="Software version")
    description: Optional[str] = Field(None, description="Human readable description")


class CpeCreate(CpeBase):
    cpe_id: str = Field(..., min_length=1, max_length=128, description="Unique CPE identifier")


class CpeUpdate(BaseModel):
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    oui: Optional[str] = Field(None, max_length=6, description="Organizationally Unique Identifier")
    product_class: Optional[str] = None
    hardware_version: Optional[str] = None
    software_version: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


class CpeResponse(CpeBase):
    cpe_id: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# -----------------------------------------------------------------------------
# Live State Schemas (Satisfying simulate_flow.sh requirements)
# -----------------------------------------------------------------------------

class CpeLiveStateResponse(BaseModel):
    cpe_id: str
    endpoint_id: Optional[str] = None
    status: str
    telemetry_metrics: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict, description="Alias for telemetry_metrics")
    current_parameters: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict, description="Alias for current_parameters")
    ip_address: Optional[str] = None
    firmware_version: Optional[str] = None
    last_seen: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# -----------------------------------------------------------------------------
# Historical Metrics Schemas
# -----------------------------------------------------------------------------

class CpeHistoryItem(BaseModel):
    id: int
    cpe_id: str
    status: str
    current_parameters: Optional[dict[str, Any]] = None
    telemetry_metrics: dict[str, Any] = Field(default_factory=dict)
    optical_power: Optional[float] = None
    recorded_at: datetime
    change_reason: str

    model_config = ConfigDict(from_attributes=True)


class CpeHistoryResponse(BaseModel):
    cpe_id: str
    total: int
    items: list[CpeHistoryItem]


# -----------------------------------------------------------------------------
# Command Dispatch Schemas
# -----------------------------------------------------------------------------

class CommandDispatchResponse(BaseModel):
    status: str = "dispatched"
    cpe_id: str
    command: str
    command_key: str
    topic: str
    dispatched_at: datetime


# -----------------------------------------------------------------------------
# Pending Command Schemas (TR-069 / Dual-Stack Command Queue)
# -----------------------------------------------------------------------------


class PendingCommandStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"

    def __str__(self) -> str:
        return str(self.value)


class PendingCommandCreate(BaseModel):
    command_type: str = Field(..., min_length=1, max_length=64, description="Command type (e.g. Reboot, GetParameterValues)")
    command_payload: dict[str, Any] = Field(default_factory=dict, description="Command payload parameters")


class PendingCommandUpdate(BaseModel):
    status: Optional[PendingCommandStatus] = Field(
        None, description="Execution status (pending, dispatched, completed, failed)"
    )
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_payload: Optional[dict[str, Any]] = None


class PendingCommandResponse(BaseModel):
    id: Union[UUID, str]
    cpe_id: str
    command_type: str
    command_payload: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_payload: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)

