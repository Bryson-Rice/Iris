import json
import logging
import threading
import time

import requests

from config import HA_HEADERS, HA_TOKEN, HA_URL, MANUAL_ENTITY_ROOMS, ROOMS

try:
    import websocket
except ImportError:
    websocket = None

logger = logging.getLogger(__name__)

DOMAIN_MAP = {
    "light":      "light",
    "switch":     "switch",
    "fan":        "fan",
    "thermostat": "climate",
    "lock":       "lock",
    "cover":      "cover",
    "media":      "media_player",
}
DOMAIN_TO_TYPE = {domain: device_type for device_type, domain in DOMAIN_MAP.items()}

REFRESH_INTERVAL = 300
KNOWN_ROOMS = list(ROOMS)
AREA_FALLBACK_NOTE = "Falling back to MANUAL_ENTITY_ROOMS + entity_id slug inference."


class EntityRegistry:
    """Maps (device type, room) and spoken names to Home Assistant entity IDs.
    Safe to call from the MQTT handler thread."""

    def __init__(self):
        self._room_index: dict[tuple[str, str], list[str]] = {}
        self._name_index: dict[str, str] = {}
        self._lock = threading.RLock()
        self._ready = threading.Event()

    def start(self) -> None:
        self._seed_from_manual_config()
        self._refresh()
        self._ready.set()
        threading.Thread(target=self._background_refresh, daemon=True).start()

    def get_entities(self, device_type: str, room: str) -> list[str]:
        """All entities of a type in a room. Falls back to '<domain>.<room>' if none are known."""
        self._ready.wait(timeout=10)
        with self._lock:
            entities = self._room_index.get((device_type, room), [])
        if entities:
            return entities

        default = f"{DOMAIN_MAP.get(device_type, device_type)}.{room}"
        logger.warning(
            f"No entities found for ({device_type!r}, {room!r}). Falling back to '{default}'. "
            "Check that HA areas are configured and the registry has synced."
        )
        return [default]

    def find_by_name(self, spoken_name: str, device_type: str | None = None) -> str | None:
        """Exact normalised match first, then substring match; the first hit wins."""
        self._ready.wait(timeout=10)
        needle = _normalise(spoken_name)
        domain = DOMAIN_MAP.get(device_type, "") if device_type else ""

        def in_domain(entity_id: str) -> bool:
            return not domain or entity_id.startswith(domain + ".")

        with self._lock:
            exact = self._name_index.get(needle)
            if exact and in_domain(exact):
                return exact

            matches = [
                entity_id for name, entity_id in self._name_index.items()
                if needle in name and in_domain(entity_id)
            ]

        if len(matches) > 1:
            logger.debug(f"Ambiguous name match for '{spoken_name}': {matches}. Using first result.")
        return matches[0] if matches else None

    def get_room_for_entity(self, entity_id: str, device_type: str | None = None) -> str | None:
        with self._lock:
            for (dtype, room), entities in self._room_index.items():
                if entity_id in entities and (device_type is None or dtype == device_type):
                    return room
        return None

    def rooms_with_device(self, device_type: str) -> list[str]:
        with self._lock:
            return sorted({room for (dtype, room) in self._room_index if dtype == device_type})

    def summary(self) -> dict:
        with self._lock:
            return {str(key): entities for key, entities in sorted(self._room_index.items())}

    def _seed_from_manual_config(self) -> None:
        # Runs before the first HA sync so commands still work if HA is unreachable at boot.
        room_index: dict[tuple[str, str], list[str]] = {}
        name_index: dict[str, str] = {}

        for entity_id, room in MANUAL_ENTITY_ROOMS.items():
            domain, slug = entity_id.split(".", 1)
            _index_entity(room_index, DOMAIN_TO_TYPE.get(domain, domain), room, entity_id)
            name_index[_normalise(slug)] = entity_id

        with self._lock:
            self._room_index = room_index
            self._name_index = name_index

        logger.info(f"EntityRegistry: seeded {len(MANUAL_ENTITY_ROOMS)} entities from MANUAL_ENTITY_ROOMS.")

    def _refresh(self) -> None:
        logger.info("EntityRegistry: syncing with Home Assistant …")
        try:
            states = self._fetch_states()
            room_index, name_index = self._build_indexes(states, self._fetch_area_map())

            # Merge rather than replace so the manually seeded entries survive.
            with self._lock:
                for (device_type, room), entities in room_index.items():
                    for entity_id in entities:
                        _index_entity(self._room_index, device_type, room, entity_id)
                self._name_index.update(name_index)

            total = sum(len(entities) for entities in room_index.values())
            logger.info(f"EntityRegistry: indexed {total} entities across {len(room_index)} room/device buckets.")
            logger.debug(f"EntityRegistry snapshot: {self.summary()}")
        except Exception as e:
            logger.error(f"EntityRegistry refresh failed: {e}", exc_info=True)

    def _background_refresh(self) -> None:
        while True:
            time.sleep(REFRESH_INTERVAL)
            self._refresh()

    def _fetch_states(self) -> list[dict]:
        resp = requests.get(f"{HA_URL}/api/states", headers=HA_HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _fetch_area_map(self) -> dict[str, str]:
        """entity_id -> area name. HA only exposes its area, device and entity
        registries over the WebSocket API; there is no REST equivalent."""
        if websocket is None:
            logger.warning(
                "EntityRegistry: 'websocket-client' isn't installed, so HA areas can't be read "
                f"(pip install websocket-client). {AREA_FALLBACK_NOTE}"
            )
            return {}

        ws_url = HA_URL.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        try:
            ws = websocket.create_connection(ws_url, timeout=10)
        except Exception as e:
            logger.warning(f"EntityRegistry: could not open a WebSocket to {ws_url} ({e}). {AREA_FALLBACK_NOTE}")
            return {}

        try:
            return self._read_areas(ws)
        except Exception as e:
            logger.warning(f"EntityRegistry: WebSocket registry fetch failed ({e}). {AREA_FALLBACK_NOTE}")
            return {}
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _read_areas(self, ws) -> dict[str, str]:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            logger.warning(f"EntityRegistry: unexpected WebSocket handshake from HA: {hello}")
            return {}

        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            logger.error(f"EntityRegistry: WebSocket authentication with HA_TOKEN failed: {auth_result}")
            return {}

        areas = self._ws_command(ws, 1, "config/area_registry/list")
        devices = self._ws_command(ws, 2, "config/device_registry/list")
        entities = self._ws_command(ws, 3, "config/entity_registry/list")
        if areas is None or devices is None or entities is None:
            return {}

        area_names = {a["area_id"]: _normalise(a["name"]).replace(" ", "_") for a in areas if a.get("area_id")}
        device_areas = {d["id"]: d["area_id"] for d in devices if d.get("area_id")}

        result: dict[str, str] = {}
        for entry in entities:
            # Most people assign areas to devices, so entities inherit their device's area.
            area_id = entry.get("area_id") or device_areas.get(entry.get("device_id"))
            if area_id in area_names:
                result[entry.get("entity_id", "")] = area_names[area_id]

        logger.info(f"EntityRegistry: resolved areas for {len(result)} entities across {len(area_names)} areas.")
        return result

    @staticmethod
    def _ws_command(ws, msg_id: int, command_type: str):
        ws.send(json.dumps({"id": msg_id, "type": command_type}))
        resp = json.loads(ws.recv())
        if not resp.get("success"):
            logger.error(f"EntityRegistry: WebSocket command {command_type!r} failed: {resp}")
            return None
        return resp.get("result", [])

    def _build_indexes(self, states: list[dict], area_map: dict[str, str]) -> tuple[dict, dict]:
        room_index: dict[tuple[str, str], list[str]] = {}
        name_index: dict[str, str] = {}

        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            if domain not in DOMAIN_TO_TYPE:
                continue

            friendly_name = state.get("attributes", {}).get("friendly_name", entity_id)
            name_index[_normalise(friendly_name)] = entity_id

            # Manual override, then the HA area, then a room prefix in the entity_id.
            room = (
                MANUAL_ENTITY_ROOMS.get(entity_id)
                or area_map.get(entity_id)
                or _infer_room_from_entity_id(entity_id)
            )
            if room:
                _index_entity(room_index, DOMAIN_TO_TYPE[domain], room, entity_id)
            else:
                logger.debug(
                    f"Could not determine room for '{entity_id}' (friendly_name='{friendly_name}'). "
                    "Assign it to an area in HA, or prefix the entity ID with the room name."
                )

        return room_index, name_index


def _index_entity(room_index: dict, device_type: str, room: str, entity_id: str) -> None:
    entities = room_index.setdefault((device_type, room), [])
    if entity_id not in entities:
        entities.append(entity_id)


def _infer_room_from_entity_id(entity_id: str) -> str | None:
    slug = entity_id.split(".", 1)[-1]
    return next((room for room in KNOWN_ROOMS if slug.startswith(room)), None)


def _normalise(name: str) -> str:
    return name.lower().strip().replace("-", " ").replace("_", " ")
