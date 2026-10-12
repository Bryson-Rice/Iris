MQTT_BROKER    = "127.0.0.1"
MQTT_PORT      = 1883
MQTT_CLIENT_ID = "iris_core"

VOICE_TOPIC       = "iris/voice/#"
SPEECH_TOPIC      = "iris/speech"
TIMER_SOUND_TOPIC = "iris/timer_sound"

# Example - HA_URL   = "http://10.0.0.1:8123"
HA_URL   = "http://CHANGE-THIS-TO-YOUR-HOME-ASSISTANT-IP:8123"
HA_TOKEN = "UPDATE-HA-TOKEN-HERE"
HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type":  "application/json",
}

# Canonical room name -> the ways it can be spoken.
ROOMS: dict[str, list[str]] = {
    "living_room":    ["living room", "lounge", "front room"],
    "bedroom":        ["bedroom", "bed room"],
    "master_bedroom": ["master", "master bedroom", "master bed"],
    "kids_room":      ["kids room", "kids bedroom"],
    "guest_room":     ["guest room", "guest bedroom"],
    "closet":         ["closet"],
    "bathroom":       ["bathroom", "bath", "half bath"],
    "kitchen":        ["kitchen"],
    "dining_room":    ["dining room", "dining"],
    "garage":         ["garage"],
    "office":         ["office"],
    "hallway":        ["hallway", "hall"],
    "basement":       ["basement"],
    "attic":          ["attic"],
    "patio":          ["patio"],
    "backyard":       ["backyard", "back yard"],
    "front_yard":     ["front yard"],
}

# Only needed for entities with no HA area whose entity_id doesn't start with a room name.
MANUAL_ENTITY_ROOMS: dict[str, str] = {
    "light.overhead_light": "living_room",
    "light.soft_light_1":   "bedroom",
    "light.soft_light_2":   "bedroom",
    "light.closet_light":   "closet",
    "light.tv_light":       "living_room",
}

COLORS_RGB: dict[str, tuple[int, int, int]] = {
    "red":        (255, 0,   0),
    "green":      (0,   255, 0),
    "blue":       (0,   0,   255),
    "white":      (255, 255, 255),
    "warm white": (255, 200, 120),
    "cool white": (200, 220, 255),
    "yellow":     (255, 255, 0),
    "orange":     (255, 128, 0),
    "purple":     (128, 0,   255),
    "pink":       (255, 105, 180),
    "cyan":       (0,   255, 255),
}

# How long a room's last intent stays available for follow-ups like "turn them off"
CONTEXT_TTL_SECONDS = 30

WEATHER_DEFAULT_CITY = "New York"
WEATHER_DEFAULT_LAT  = 40.712
WEATHER_DEFAULT_LON  = -74.006
WEATHER_TIMEZONE     = "America/New_York"

LOG_LEVEL  = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
