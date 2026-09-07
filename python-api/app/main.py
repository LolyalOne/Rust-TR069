"""TR-369 USP ACS Manager FastAPI application entry point."""

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
app.include_router(health.router, prefix=settings.API_V1_PREFIX)  # /api/v1/health
app.include_router(cpes.router, prefix=settings.API_V1_PREFIX)  # /api/v1/cpes
