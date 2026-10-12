import logging

import requests

from config import COLORS_RGB, HA_HEADERS, HA_URL
from entity_registry import EntityRegistry

logger = logging.getLogger(__name__)

# (device, action) -> HA service, for actions that need no extra payload.
SIMPLE_SERVICES: dict[tuple[str, str], str] = {
    ("light", "turn_off"):      "light/turn_off",
    ("switch", "turn_on"):      "switch/turn_on",
    ("switch", "turn_off"):     "switch/turn_off",
    ("fan", "turn_on"):         "fan/turn_on",
    ("fan", "turn_off"):        "fan/turn_off",
    ("lock", "lock"):           "lock/lock",
    ("lock", "unlock"):         "lock/unlock",
    ("cover", "open"):          "cover/open_cover",
    ("cover", "close"):         "cover/close_cover",
    ("media", "play"):          "media_player/media_play",
    ("media", "pause"):         "media_player/media_pause",
    ("media", "volume_up"):     "media_player/volume_up",
    ("media", "volume_down"):   "media_player/volume_down",
}

BRIGHTNESS_STEP_PCT = 25
THERMOSTAT_STEP_DEGREES = 2


class HomeAssistantController:
    def __init__(self, registry: EntityRegistry):
        self.registry = registry
        self._handlers = {
            ("light", "turn_on"):             self._light_turn_on,
            ("light", "dim"):                 lambda e, p: self._shift_brightness(e, p, -BRIGHTNESS_STEP_PCT),
            ("light", "brighten"):            lambda e, p: self._shift_brightness(e, p, BRIGHTNESS_STEP_PCT),
            ("light", "set_brightness"):      self._light_set_brightness,
            ("fan", "set_brightness"):        self._fan_set_percentage,
            ("thermostat", "set_temperature"): self._thermostat_set,
            ("thermostat", "increase_temp"):  lambda e, p: self._nudge_thermostat(e, THERMOSTAT_STEP_DEGREES),
            ("thermostat", "decrease_temp"):  lambda e, p: self._nudge_thermostat(e, -THERMOSTAT_STEP_DEGREES),
            ("media", "mute"):                self._media_mute,
        }

    def execute(self, intent: dict) -> bool:
        device = intent.get("device")
        action = intent.get("action")
        room = intent.get("room")
        params = intent.get("parameters", {})
        target_name = intent.get("target_name")

        if not device or not action:
            logger.warning(f"Incomplete intent — device={device}, action={action}. Nothing sent to HA.")
            return False

        entities = self._resolve_targets(device, room, target_name)
        if not entities:
            logger.error(
                f"No entities found for device={device!r} room={room!r} target={target_name!r}. Aborting."
            )
            return False

        # A named device can live in a different room than the satellite that heard
        # the command; correct the room so the spoken response names the real one.
        if target_name:
            actual_room = self.registry.get_room_for_entity(entities[0], device)
            if actual_room and actual_room != room:
                logger.debug(f"Correcting spoken room: '{room}' → '{actual_room}'")
                intent["room"] = actual_room

        logger.info(f"Executing '{action}' on {entities} (target_name={target_name!r}, params={params})")

        results = []
        for entity in entities:
            try:
                results.append(self._dispatch(device, action, entity, params))
            except Exception as e:
                logger.error(f"Exception executing {action} on {entity}: {e}", exc_info=True)
                results.append(False)
        return all(results)

    def _resolve_targets(self, device: str, room: str, target_name: str | None) -> list[str]:
        if target_name:
            entity = self.registry.find_by_name(target_name, device)
            if entity:
                logger.debug(f"Specific target resolved: '{target_name}' → {entity}")
                return [entity]
            logger.warning(
                f"Could not find a specific entity for '{target_name}'. "
                f"Falling back to all {device}s in '{room}'."
            )
        return self.registry.get_entities(device, room)

    def _dispatch(self, device: str, action: str, entity: str, params: dict) -> bool:
        service = SIMPLE_SERVICES.get((device, action))
        if service:
            return self._call_service(service, entity)

        handler = self._handlers.get((device, action))
        if handler is None:
            logger.warning(f"No handler for action '{action}' on device type '{device}'.")
            return False
        return handler(entity, params)

    def _light_turn_on(self, entity: str, params: dict) -> bool:
        payload: dict = {}
        if "brightness" in params:
            payload["brightness"] = int(params["brightness"] / 100 * 255)
        if "color_temp_kelvin" in params:
            payload["color_temp_kelvin"] = params["color_temp_kelvin"]
        elif "color" in params:
            rgb = COLORS_RGB.get(params["color"].lower())
            if rgb:
                payload["rgb_color"] = list(rgb)
        return self._call_service("light/turn_on", entity, **payload)

    def _light_set_brightness(self, entity: str, params: dict) -> bool:
        pct = params.get("brightness", 50)
        return self._call_service("light/turn_on", entity, brightness=int(pct / 100 * 255))

    def _shift_brightness(self, entity: str, params: dict, step_pct: int) -> bool:
        """An explicit brightness ("dim to 10") is absolute; otherwise step from the current level."""
        if "brightness" in params:
            pct = max(0, min(100, params["brightness"]))
        else:
            current = self._get_state_attribute(entity, "brightness")
            current_pct = round(current / 255 * 100) if current else 50
            pct = max(0, min(100, current_pct + step_pct))
        return self._call_service("light/turn_on", entity, brightness=int(pct / 100 * 255))

    def _fan_set_percentage(self, entity: str, params: dict) -> bool:
        return self._call_service("fan/set_percentage", entity, percentage=params.get("brightness", 50))

    def _thermostat_set(self, entity: str, params: dict) -> bool:
        temp = params.get("temperature")
        if temp is None:
            logger.warning("set_temperature called with no temperature param.")
            return False
        return self._call_service("climate/set_temperature", entity, temperature=temp)

    def _nudge_thermostat(self, entity: str, delta: int) -> bool:
        current = self._get_state_attribute(entity, "temperature")
        if current is None:
            logger.warning("Could not read current thermostat temperature.")
            return False
        return self._call_service("climate/set_temperature", entity, temperature=current + delta)

    def _media_mute(self, entity: str, params: dict) -> bool:
        return self._call_service("media_player/volume_mute", entity, is_volume_muted=True)

    def _call_service(self, service: str, entity_id: str, **kwargs) -> bool:
        url = f"{HA_URL}/api/services/{service}"
        payload = {"entity_id": entity_id, **kwargs}
        try:
            resp = requests.post(url, headers=HA_HEADERS, json=payload, timeout=5)
        except requests.exceptions.ConnectionError:
            logger.error("Could not reach Home Assistant (connection refused).")
            return False
        except requests.exceptions.Timeout:
            logger.error("Home Assistant request timed out.")
            return False

        if resp.status_code in (200, 201):
            logger.debug(f"HA OK  [{resp.status_code}] {service} → {entity_id}")
            return True
        logger.error(f"HA error [{resp.status_code}] {service} → {entity_id}: {resp.text}")
        return False

    def _get_state_attribute(self, entity_id: str, attribute: str):
        url = f"{HA_URL}/api/states/{entity_id}"
        try:
            resp = requests.get(url, headers=HA_HEADERS, timeout=5)
            if resp.status_code == 200:
                return resp.json().get("attributes", {}).get(attribute)
        except Exception as e:
            logger.error(f"Error fetching state for {entity_id}: {e}")
        return None
