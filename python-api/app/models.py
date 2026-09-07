"""Declarative ORM models for TR-369 USP ACS Manager."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Dialect-agnostic JSON type that uses JSONB on PostgreSQL and JSON elsewhere
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")
BIGINT_TYPE = BigInteger().with_variant(Integer, "sqlite")


class CpeInventory(Base):
    """Authoritative persistent registry of CPE devices."""
    __tablename__ = "cpe_inventory"

    cpe_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    serial_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    manufacturer: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    oui: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    product_class: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    hardware_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    software_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="offline", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    live_state: Mapped[Optional["CpeLiveState"]] = relationship(
        "CpeLiveState",
        back_populates="inventory",
        cascade="all, delete-orphan",
        uselist=False,
    )
    historical_metrics: Mapped[list["CpeHistoricalMetrics"]] = relationship(
        "CpeHistoricalMetrics",
        back_populates="inventory",
        cascade="all, delete-orphan",
        order_by="desc(CpeHistoricalMetrics.recorded_at)",
    )


class CpeLiveState(Base):
    """In-RAM volatile live state table (ram_tablespace tmpfs)."""
    __tablename__ = "cpe_live_state"

    cpe_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("cpe_inventory.cpe_id", ondelete="CASCADE"),
        primary_key=True,
    )
    endpoint_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    current_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        server_default="{}",
        default=dict,
        nullable=False,
    )
    telemetry_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        server_default="{}",
        default=dict,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="offline", nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationship
    inventory: Mapped["CpeInventory"] = relationship("CpeInventory", back_populates="live_state")


class CpeHistoricalMetrics(Base):
    """Persistent audit log and time-series history."""
    __tablename__ = "cpe_historical_metrics"

    id: Mapped[int] = mapped_column(BIGINT_TYPE, primary_key=True, autoincrement=True)
    cpe_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("cpe_inventory.cpe_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_parameters: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON_TYPE,
        server_default="{}",
        default=dict,
        nullable=True,
    )
    telemetry_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE,
        server_default="{}",
        default=dict,
        nullable=False,
    )
    optical_power: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    change_reason: Mapped[str] = mapped_column(
        String(64),
        default="optical_signal_variation",
        nullable=False,
    )

    # Relationship
    inventory: Mapped["CpeInventory"] = relationship("CpeInventory", back_populates="historical_metrics")
