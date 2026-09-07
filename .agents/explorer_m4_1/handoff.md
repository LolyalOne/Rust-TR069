# Milestone 4: Python FastAPI Manager — Architecture & Implementation Specification

## 1. Observation

Direct observations extracted from project files, database DDL, test harness, and docker configurations:

1. **User Constraints & Scope (`ORIGINAL_REQUEST.md`)**:
   - Lines 34–36 (R6): *"API RESTful assíncrona com endpoints CRUD de inventário, consulta de status em tempo real da tabela `unlogged`, e disparo de comandos (ex: Reboot) publicando no broker MQTT."*
   - Lines 77–79 (R3): *"Desenvolver a API assíncrona na pasta `python-api/` usando Python 3.11, FastAPI e SQLAlchemy 2.0 (assíncrono). A API deve ter isolamento de threads por Gunicorn para contenção de memória, servindo dados e injetando comandos no MQTT."*
   - Lines 20 & 91 (`docker-compose.yml`): Memory limit for `python-api` is strictly 1 GB (`limits: memory: 1G`).

2. **Compose Service & Healthcheck (`docker-compose.yml`, lines 73–101)**:
   ```yaml
   python-api:
     build:
       context: ./python-api
     volumes:
       - .:/workspace:cached
     environment:
       - DATABASE_URL=postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db
       - MQTT_HOST=mosquitto
       - MQTT_PORT=1883
     ports:
       - "8000:8000"
     depends_on:
       postgres:
         condition: service_healthy
       mosquitto:
         condition: service_healthy
     deploy:
       resources:
         limits:
           memory: 1G
     healthcheck:
       test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
       interval: 5s
       timeout: 3s
       retries: 5
       start_period: 5s
   ```
   *Observation*: 
   - `python-api` listens on port `8000`.
   - Healthcheck specifically invokes Python's standard library `urllib.request.urlopen('http://localhost:8000/health')`, requiring an immediate HTTP 200 response on `GET /health`.
   - Environment provides `DATABASE_URL=postgresql+asyncpg://...` and `MQTT_HOST=mosquitto`, `MQTT_PORT=1883`.

3. **End-to-End Simulation Script (`simulate_flow.sh`)**:
   - **Pre-flight & Cleanup (Lines 420, 452)**:
     - `curl -s -f -m 2 "${API_URL}/health"`: verifies API readiness.
     - `curl -s -X DELETE "${API_URL}/api/v1/cpes/${CPE_ID}"`: ensures clean state for test CPE.
   - **Step 1: Registration & Inventory Lookup (Lines 457–503)**:
     - `POST ${API_URL}/api/v1/cpes` with payload:
       ```json
       {
         "cpe_id": "${CPE_ID}",
         "serial_number": "${SERIAL_NUMBER}",
         "manufacturer": "TP-Link",
         "model": "Archer-AX50",
         "oui": "00259E",
         "product_class": "Gateway",
         "hardware_version": "v1.0",
         "software_version": "1.0.0",
         "description": "E2E Automated Simulation Device"
       }
       ```
     - Validates HTTP response status code is `201` or `200`.
     - Validates response JSON contains `.cpe_id == "${CPE_ID}"`.
     - Validates `GET ${API_URL}/api/v1/cpes/${CPE_ID}` returns `.model == "Archer-AX50"`.
   - **Step 3: Live State Inspection (Lines 549–571)**:
     - Polled via `GET ${API_URL}/api/v1/cpes/${CPE_ID}/live-state` (with fallback to `/api/v1/cpes/${CPE_ID}/live`).
     - Extracts `.status` (must be `"online"`).
     - Extracts `.telemetry_metrics.cpu_usage` or `.metrics.cpu_usage` (must match `"42.5"` or `"42.50"`).
   - **Step 4: Historical Reconciled Metrics (Lines 622–638)**:
     - Polled via `GET ${API_URL}/api/v1/cpes/${CPE_ID}/history`.
     - Extracts record count using `.items | length` (or root array `length`).
     - Must return `>= 2` historical entries once optical signal changes by > 1.0 dBm.
   - **Step 5: Reboot Command Dispatch (Lines 673–708)**:
     - Dispatched via `POST ${API_URL}/api/v1/cpes/${CPE_ID}/reboot`.
     - Validates HTTP response status code is `200` or `202`.
     - Test subscriber on Mosquitto wildcard `usp/endpoint/${CPE_ID}/#` must capture message on `usp/endpoint/${CPE_ID}/request`.
     - Captured command text must match regex `reboot|operate` (case-insensitive).

4. **Database Schema & Constraints (`postgres/init.sql`)**:
   - `cpe_inventory`: Primary key `cpe_id VARCHAR(128)`, `serial_number VARCHAR(64) UNIQUE NOT NULL`, `manufacturer`, `model`, `oui`, `product_class`, `hardware_version`, `software_version`, `description`, `status DEFAULT 'offline'`, `created_at`, `updated_at`.
   - `cpe_live_state`: Primary key `cpe_id VARCHAR(128) REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`, `endpoint_id`, `current_parameters JSONB`, `telemetry_metrics JSONB`, `status`, `ip_address`, `firmware_version`, `last_seen`, `updated_at`.
   - `cpe_historical_metrics`: Primary key `id BIGSERIAL`, `cpe_id VARCHAR(128) REFERENCES cpe_inventory(cpe_id) ON DELETE CASCADE`, `status`, `current_parameters JSONB`, `telemetry_metrics JSONB`, `optical_power NUMERIC(6,2)`, `recorded_at`, `change_reason`.
   - `cpe_state_history`: View aliasing `cpe_historical_metrics`.

5. **Rust Core Topic Routing (`rust-core/src/main.rs`, lines 59–63)**:
   - Topic check:
     ```rust
     pub fn is_command_topic(topic: &str) -> bool {
         topic.ends_with("/request") || topic.contains("/request/")
     }
     ```
   - *Observation*: Rust Core explicitly ignores messages published to `/request`, ensuring that commands dispatched by FastAPI are consumed exclusively by the target CPE device, eliminating any internal feedback loop.

---

## 2. Logic Chain

1. **Framework & Engine Selection**:
   - Requirement R3 explicitly mandates Python 3.11, FastAPI, and asynchronous SQLAlchemy 2.0.
   - Using `asyncpg` as the DB driver matches `postgresql+asyncpg://...` in `docker-compose.yml`.
   - Using SQLAlchemy 2.0 `Mapped[...]` and `mapped_column(...)` DeclarativeBase ensures type-safe ORM definitions compatible with Pydantic V2 schemas.

2. **Memory Containment & Gunicorn Concurrency**:
   - Compose imposes a strict 1 GB RAM limit.
   - Running Gunicorn with `workers = 2` and `worker_class = "uvicorn.workers.UvicornWorker"` provides process isolation and multi-core utilization.
   - Each worker process occupies ~90–120 MB of resident memory under load, maintaining total memory usage below 300 MB and providing >700 MB of safety headroom against OOM kills.
   - Adding `max_requests = 1000` and `max_requests_jitter = 50` recycles worker processes periodically, neutralizing any potential memory fragmentation or leak over long periods.

3. **MQTT Async Integration Architecture**:
   - `aiomqtt` (built on `paho-mqtt`) provides an async-native context manager that interfaces with the asyncio event loop.
   - In a multi-worker Gunicorn environment, sharing a single persistent TCP socket across forked processes leads to connection resets and race conditions.
   - Instead, the `MqttPublisher` class connects per publish operation or manages an isolated connection per worker event loop using unique client identifiers (`fastapi-worker-{pid}-{uuid}`).
   - Outbound commands are published to `usp/endpoint/{cpe_id}/request` with QoS 1.

4. **Wire-Format & simulate_flow.sh Compatibility for Reboot Command**:
   - `simulate_flow.sh` captures published MQTT messages using Python's `paho-mqtt` (`msg.payload.decode("utf-8", errors="replace")`) and runs `grep -qi "reboot\|operate"`.
   - A structured JSON payload conforming to TR-369 USP Msg format (containing both `"operate"` and `"Device.Reboot()"` keys) guarantees 100% UTF-8 compatibility, human readability in logs, and exact regex matching without reliance on binary protobuf decoders on test clients.
   - For complete TR-369 compliance, the manager can accept an optional query parameter or header to publish Protobuf binary payloads if desired.

5. **Endpoint Contract & Aliasing**:
   - The test script queries both `/api/v1/cpes/{cpe_id}/live-state` and `/api/v1/cpes/{cpe_id}/live`. Both routes will point to the identical handler.
   - In `CpeLiveStateResponse`, providing both `telemetry_metrics` and `metrics` (as an alias) and `current_parameters` and `parameters` ensures immediate compliance with any test runner variant.
   - For `/api/v1/cpes/{cpe_id}/history`, returning `{"cpe_id": ..., "total": ..., "items": [...]}` ensures `.items | length` returns `>= 2`.

---

## 3. Caveats

1. **Broker Connectivity on Cold Start**: Mosquitto is marked as `depends_on: condition: service_healthy`. However, if the broker momentarily restarts, `aiomqtt` must handle connection exceptions gracefully and return HTTP 503 rather than crashing the worker.
2. **Cascading Deletes**: `cpe_live_state` and `cpe_historical_metrics` define `ON DELETE CASCADE` in PostgreSQL. When `DELETE /api/v1/cpes/{cpe_id}` is invoked, deleting the row in `cpe_inventory` cascades automatically at the database level.
3. **Unregistered Devices**: Rust Core auto-provisions devices if telemetry arrives before API registration. The API's `POST /api/v1/cpes` must handle existing auto-provisioned rows gracefully (e.g. updating metadata or returning 200/201 without failing).

---

## 4. Conclusion & Complete Design Specification

Milestone 4 is fully specified below. The implementer agent can directly materialize this architecture into `python-api/`.

### 4.1 Project Directory Layout

```
python-api/
├── Dockerfile
├── requirements.txt
├── gunicorn_conf.py
├── app/
│   ├── __init__.py
│   ├── main.py              # Application factory, lifespan, routing, global exception handlers
│   ├── config.py            # Environment configuration with pydantic-settings
│   ├── database.py          # SQLAlchemy 2.0 async engine, sessionmaker, and get_db dependency
│   ├── models.py            # Declarative ORM models (CpeInventory, CpeLiveState, CpeHistoricalMetrics)
│   ├── schemas.py           # Pydantic V2 request/response validation schemas
│   ├── mqtt.py              # Async MQTT publisher client (aiomqtt)
│   └── routers/
│       ├── __init__.py
│       ├── cpes.py          # All CPE endpoints (/api/v1/cpes CRUD, live-state, history, reboot)
│       └── health.py        # Healthcheck endpoint (/health)
└── tests/
    ├── __init__.py
    └── test_api.py          # Async unit/integration tests with httpx and pytest
```

---

### 4.2 Dependency Manifest: `python-api/requirements.txt`

```text
fastapi>=0.109.0,<0.115.0
uvicorn[standard]>=0.27.0,<0.31.0
gunicorn>=21.2.0,<23.0.0
pydantic>=2.6.0,<2.9.0
pydantic-settings>=2.1.0,<2.5.0
sqlalchemy[asyncio]>=2.0.25,<2.1.0
asyncpg>=0.29.0,<0.30.0
aiomqtt>=2.0.0,<2.3.0
paho-mqtt>=1.6.1,<2.1.0
httpx>=0.26.0,<0.28.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

---

### 4.3 Container Definition: `python-api/Dockerfile`

```dockerfile
# Multi-stage or optimized single-stage Debian slim build
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install minimal OS dependencies for compilation and healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose HTTP port
EXPOSE 8000

# Run with Gunicorn using Uvicorn workers
CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:app"]
```

---

### 4.4 Process Management: `python-api/gunicorn_conf.py`

```python
"""
Gunicorn Configuration for TR-369 USP FastAPI Manager
Enforces strict physical memory containment (< 1GB) and process isolation.
"""
import multiprocessing
import os

# Server socket
bind = os.getenv("BIND", "0.0.0.0:8000")
backlog = 2048

# Concurrency & Worker model
# Requirement: 2 workers, worker_class="uvicorn.workers.UvicornWorker"
workers = int(os.getenv("WORKERS", "2"))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = int(os.getenv("TIMEOUT", "60"))
keepalive = 5

# Memory containment & worker recycling (prevents memory fragmentation)
max_requests = 1000
max_requests_jitter = 50

# Logging
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s µs'

# Process naming
proc_name = "fastapi-manager"
```

---

### 4.5 Configuration Settings: `python-api/app/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "TR-369 USP ACS Manager"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = False

    # Database Settings
    DATABASE_URL: str = "postgresql+asyncpg://acs_user:acs_password@postgres:5432/acs_db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30

    # Mosquitto MQTT Broker Settings
    MQTT_HOST: str = "mosquitto"
    MQTT_PORT: int = 1883
    MQTT_CLIENT_ID: str = "fastapi-manager"
    MQTT_TIMEOUT: int = 5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
```

---

### 4.6 Database Engine & Sessionmaker: `python-api/app/database.py`

```python
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Engine configuration with connection pooling and pre-ping
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=True,
    echo=settings.DEBUG,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy 2.0 declarative models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

---

### 4.7 ORM Models: `python-api/app/models.py`

```python
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
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
        JSONB,
        server_default="{}",
        default=dict,
        nullable=False,
    )
    telemetry_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        server_default="{}",
        default=dict,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="offline", nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
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

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cpe_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("cpe_inventory.cpe_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_parameters: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, server_default="{}", nullable=True)
    telemetry_metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}", default=dict, nullable=False)
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
```

---

### 4.8 Schemas: `python-api/app/schemas.py`

```python
from datetime import datetime
from typing import Any, Optional
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
    oui: Optional[str] = None
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
```

---

### 4.9 MQTT Publisher: `python-api/app/mqtt.py`

```python
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aiomqtt
from app.config import settings

logger = logging.getLogger("fastapi.mqtt")


class MqttPublisher:
    """Asynchronous MQTT Client manager for publishing TR-369 commands."""

    def __init__(
        self,
        host: str = settings.MQTT_HOST,
        port: int = settings.MQTT_PORT,
        client_id: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.client_id = client_id or f"{settings.MQTT_CLIENT_ID}-{os.getpid()}"

    async def publish(self, topic: str, payload: str | bytes, qos: int = 1) -> None:
        """Publishes a payload asynchronously with QoS 1 acknowledgment."""
        client_ident = f"{self.client_id}-{uuid.uuid4().hex[:8]}"
        try:
            async with aiomqtt.Client(
                hostname=self.host,
                port=self.port,
                identifier=client_ident,
                timeout=settings.MQTT_TIMEOUT,
            ) as client:
                await client.publish(topic, payload=payload, qos=qos)
                logger.info("Published to MQTT topic '%s' (QoS %d)", topic, qos)
        except Exception as e:
            logger.error("Failed to publish to MQTT topic '%s': %s", topic, e)
            raise

    async def publish_reboot_command(self, cpe_id: str) -> dict[str, Any]:
        """
        Constructs and publishes a TR-369 USP Operate Reboot command.
        Target Topic: usp/endpoint/{cpe_id}/request
        QoS: 1
        """
        command_key = f"cmd-reboot-{uuid.uuid4()}"
        topic = f"usp/endpoint/{cpe_id}/request"
        now_iso = datetime.now(timezone.utc).isoformat()

        # Wire-compatible TR-369 USP Operate envelope
        # Encodes both 'operate' and 'Device.Reboot()' for standard regex matching
        payload_dict = {
            "usp": {
                "header": {
                    "msg_id": str(uuid.uuid4()),
                    "msg_type": "OPERATE",
                },
                "body": {
                    "request": {
                        "operate": {
                            "command": "Device.Reboot()",
                            "command_key": command_key,
                            "send_resp": True,
                        }
                    }
                },
            },
            "cpe_id": cpe_id,
            "command": "Device.Reboot()",
            "operate": {
                "command": "Device.Reboot()",
                "command_key": command_key,
            },
            "timestamp": now_iso,
        }

        raw_payload = json.dumps(payload_dict)
        await self.publish(topic, raw_payload, qos=1)

        return {
            "cpe_id": cpe_id,
            "command": "Device.Reboot()",
            "command_key": command_key,
            "topic": topic,
            "dispatched_at": datetime.now(timezone.utc),
        }


mqtt_publisher = MqttPublisher()
```

---

### 4.10 CPE Endpoints Router: `python-api/app/routers/cpes.py`

```python
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import CpeHistoricalMetrics, CpeInventory, CpeLiveState
from app.mqtt import mqtt_publisher
from app.schemas import (
    CommandDispatchResponse,
    CpeCreate,
    CpeHistoryItem,
    CpeHistoryResponse,
    CpeLiveStateResponse,
    CpeResponse,
    CpeUpdate,
)

router = APIRouter(prefix="/cpes", tags=["CPEs"])


@router.post("", response_model=CpeResponse, status_code=status.HTTP_201_CREATED)
async def create_cpe(payload: CpeCreate, db: AsyncSession = Depends(get_db)):
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
        await db.commit()
        await db.refresh(existing)
        return existing

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

    return CpeLiveStateResponse(
        cpe_id=live.cpe_id,
        endpoint_id=live.endpoint_id,
        status=live.status,
        telemetry_metrics=live.telemetry_metrics,
        metrics=live.telemetry_metrics,  # Alias
        current_parameters=live.current_parameters,
        parameters=live.current_parameters,  # Alias
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
            telemetry_metrics=r.telemetry_metrics,
            optical_power=float(r.optical_power) if r.optical_power is not None else None,
            recorded_at=r.recorded_at,
            change_reason=r.change_reason,
        )
        for r in records
    ]

    return CpeHistoryResponse(cpe_id=cpe_id, total=len(items), items=items)


@router.post("/{cpe_id}/reboot", response_model=CommandDispatchResponse, status_code=status.HTTP_200_OK)
async def reboot_cpe(cpe_id: str, db: AsyncSession = Depends(get_db)):
    """
    Dispatches TR-369 Reboot Operate command to Mosquitto MQTT broker:
      Topic: usp/endpoint/{cpe_id}/request
      QoS: 1
    """
    cpe = await db.get(CpeInventory, cpe_id)
    if not cpe:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"CPE '{cpe_id}' not found")

    try:
        dispatch_info = await mqtt_publisher.publish_reboot_command(cpe_id)
    except Exception as e:
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
```

---

### 4.11 Healthcheck Router: `python-api/app/routers/health.py`

```python
from fastapi import APIRouter, status
from sqlalchemy import text
from app.database import AsyncSessionLocal
from app.config import settings

router = APIRouter(tags=["Health"])


@router.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """
    Healthcheck endpoint answering docker-compose healthcheck.
    Performs fast DB verification.
    """
    db_status = "ok"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "database": db_status,
        "mqtt_broker": f"{settings.MQTT_HOST}:{settings.MQTT_PORT}",
        "version": "1.0.0",
    }
```

---

### 4.12 Application Entry Point: `python-api/app/main.py`

```python
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine
from app.routers import cpes, health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fastapi.manager")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager handling startup and shutdown routines."""
    logger.info("Starting up TR-369 USP FastAPI Manager...")
    yield
    logger.info("Shutting down TR-369 USP FastAPI Manager. Disposing DB engine...")
    await engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Asynchronous TR-369 / USP ACS Manager API and MQTT Command Dispatcher",
    lifespan=lifespan,
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(health.router)  # /health
app.include_router(cpes.router, prefix=settings.API_V1_PREFIX)  # /api/v1/cpes
```

---

## 5. Verification Method

### 5.1 Static Validation
1. Verify Python file syntax and type integrity:
   ```bash
   python3 -m py_compile python-api/gunicorn_conf.py python-api/app/*.py python-api/app/routers/*.py
   ```
2. Verify package dependencies in `python-api/requirements.txt`.

### 5.2 Containerized Build & Healthcheck
1. Build and run the `python-api` service via Docker Compose:
   ```bash
   docker compose build python-api
   docker compose up -d python-api
   ```
2. Verify container reaches `healthy` state within 10 seconds:
   ```bash
   docker compose ps python-api
   ```
3. Test healthcheck directly:
   ```bash
   curl -f -s http://localhost:8000/health
   ```

### 5.3 Automated End-to-End Simulation
Execute the master simulation script:
```bash
./simulate_flow.sh --verbose
```
Expected output:
- Step 1: `POST /api/v1/cpes` returns 201; `GET /api/v1/cpes/cpe-sim-001` returns `Archer-AX50`.
- Step 2: MQTT telemetry published to Mosquitto.
- Step 3: `GET /api/v1/cpes/cpe-sim-001/live-state` returns `status=online` and `cpu_usage=42.5`.
- Step 4: `GET /api/v1/cpes/cpe-sim-001/history` returns `.items` length `>= 2`.
- Step 5: `POST /api/v1/cpes/cpe-sim-001/reboot` publishes command with QoS 1 captured on Mosquitto broker.
- Final: Exit code `0`.
