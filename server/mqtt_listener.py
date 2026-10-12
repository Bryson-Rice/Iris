import json
import logging
import time
from datetime import datetime

import paho.mqtt.client as mqtt

from config import MQTT_BROKER, MQTT_CLIENT_ID, MQTT_PORT, SPEECH_TOPIC, VOICE_TOPIC
from ha_controller import HomeAssistantController
from intent_engine import IntentEngine
from response_engine import ResponseEngine
from scene_engine import SceneEngine
from timer_engine import TimerEngine
from weather_engine import WeatherEngine

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECS = 5
CITY_NOT_HEARD = "Sorry, I didn't catch the city name."


def spoken_time() -> str:
    now = datetime.now()
    hour = now.strftime("%-I")
    minute = now.strftime("%M")
    period = now.strftime("%p")

    if minute == "00":
        return f"It's {hour} o'clock {period}."
    if minute.startswith("0"):
        return f"It's {hour} oh {minute[1]} {period}."
    return f"It's {hour} {minute} {period}."


class MQTTListener:
    def __init__(
        self,
        intent_engine: IntentEngine,
        ha_controller: HomeAssistantController,
        response_engine: ResponseEngine,
        scene_engine: SceneEngine,
        weather_engine: WeatherEngine,
        timer_engine: TimerEngine,
    ):
        self.intent_engine = intent_engine
        self.ha_controller = ha_controller
        self.response_engine = response_engine
        self.scene_engine = scene_engine
        self.weather_engine = weather_engine
        self.timer_engine = timer_engine

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            clean_session=True,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=RECONNECT_DELAY_SECS)

    def start(self) -> None:
        logger.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT} …")
        self._connect_with_retry()
        self.client.loop_forever()

    def stop(self) -> None:
        logger.info("Stopping MQTT listener …")
        self.client.loop_stop()
        self.client.disconnect()

    def _connect_with_retry(self) -> None:
        attempts = 0
        while True:
            try:
                self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
                return
            except OSError as e:
                attempts += 1
                logger.warning(
                    f"MQTT connection attempt {attempts} failed: {e}. Retrying in {RECONNECT_DELAY_SECS}s …"
                )
                time.sleep(RECONNECT_DELAY_SECS)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            logger.error(f"MQTT connection refused: {reason_code}")
            return
        logger.info("Connected to MQTT broker.")
        # QoS 1 so no voice command is silently dropped.
        client.subscribe(VOICE_TOPIC, qos=1)
        logger.info(f"Subscribed to '{VOICE_TOPIC}'.")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            logger.info("Disconnected from MQTT broker (clean).")
        else:
            # paho reconnects on its own; this is just for visibility.
            logger.warning(f"Unexpected disconnect (rc={reason_code}). Reconnecting in {RECONNECT_DELAY_SECS}s …")

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        topic = msg.topic
        raw = msg.payload.decode("utf-8", errors="replace")
        logger.debug(f"MQTT message on '{topic}': {raw}")

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON on '{topic}': {e}  — payload: {raw!r}")
            return

        text = payload.get("text", "").strip()
        room = payload.get("room", "").strip()

        if not text:
            logger.warning(f"Empty 'text' field from '{topic}'; ignoring.")
            return
        if not room:
            parts = topic.split("/")
            room = parts[-1] if len(parts) >= 3 else "unknown"
            logger.warning(f"No 'room' in payload; derived '{room}' from topic.")

        self._handle_command(client, room, text)

    def _handle_command(self, client: mqtt.Client, room: str, text: str) -> None:
        logger.info(f"[{room}] Command received: '{text}'")

        try:
            response = self._build_response(room, text)
        except Exception as e:
            logger.error(f"[{room}] Unhandled exception processing command '{text}': {e}", exc_info=True)
            response = "Sorry, something went wrong."

        client.publish(f"{SPEECH_TOPIC}/{room}", response, qos=1)
        logger.info(f"[{room}] Response: {response}")

    def _build_response(self, room: str, text: str) -> str:
        # Scenes take priority over normal intents.
        scene = self.scene_engine.match(text)
        if scene:
            self.scene_engine.execute(scene)
            return self.scene_engine.get_response(scene)

        intent = self.intent_engine.parse(text, room)
        action = intent.get("action")
        city = intent.get("parameters", {}).get("city")

        # The satellite's vocabulary is closed; a city it didn't recognise arrives as [unk].
        if city and "[unk]" in city:
            return CITY_NOT_HEARD

        if action == "tell_time":
            return spoken_time()
        if action == "weather_temperature":
            return self.weather_engine.current_temperature(city)
        if action == "weather_today":
            return self.weather_engine.daily_forecast(city)
        if action == "weather_riding":
            return self.weather_engine.riding_assessment(city)
        if action == "timer_set":
            return self.timer_engine.set_timer(text, room)
        if action == "timer_cancel":
            return self.timer_engine.cancel_timer(text)
        if action == "timer_status":
            return self.timer_engine.timer_status()

        success = self.ha_controller.execute(intent)
        return self.response_engine.generate(intent, success)
