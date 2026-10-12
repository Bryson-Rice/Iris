import logging
import signal
import sys

import config
from context_manager import ContextManager
from entity_registry import EntityRegistry
from ha_controller import HomeAssistantController
from intent_engine import IntentEngine
from mqtt_listener import MQTTListener
from response_engine import ResponseEngine
from scene_engine import SceneEngine
from timer_engine import TimerEngine
from weather_engine import WeatherEngine

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.INFO), format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

_listener: MQTTListener | None = None


def _shutdown(sig, frame) -> None:
    logger.info(f"Received signal {sig}. Shutting down Iris …")
    if _listener:
        _listener.stop()
    sys.exit(0)


def main() -> None:
    global _listener

    logger.info("Iris — self-hosted smart home voice assistant")
    logger.info(f"  MQTT broker : {config.MQTT_BROKER}:{config.MQTT_PORT}")
    logger.info(f"  HA endpoint : {config.HA_URL}")
    logger.info(f"  Context TTL : {config.CONTEXT_TTL_SECONDS}s")

    registry = EntityRegistry()
    registry.start()

    weather_engine = WeatherEngine()
    ha_controller = HomeAssistantController(registry)

    _listener = MQTTListener(
        intent_engine=IntentEngine(ContextManager()),
        ha_controller=ha_controller,
        response_engine=ResponseEngine(),
        scene_engine=SceneEngine(ha_controller, registry, weather_engine),
        weather_engine=weather_engine,
        timer_engine=TimerEngine(),
    )

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _listener.start()


if __name__ == "__main__":
    main()
