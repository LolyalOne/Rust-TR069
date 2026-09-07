"""Configuration settings for TR-369 USP ACS Manager."""

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
        extra="ignore",
    )


settings = Settings()
