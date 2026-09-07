"""Asynchronous MQTT publisher for TR-369 USP command dispatch."""

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
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        # Wire-compatible TR-369 USP Operate envelope
        # Encodes both 'operate' and 'Device.Reboot()' for regex and parser matching
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
            "dispatched_at": now_dt,
        }


mqtt_publisher = MqttPublisher()
