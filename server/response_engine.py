import logging
import random

logger = logging.getLogger(__name__)

# Keyed by (device, action); one template is picked at random per response.
RESPONSES: dict[tuple[str, str], list[str]] = {
    ("light", "turn_on"): [
        "Turning on the {room} lights.",
        "The {room} lights are on.",
        "Sure, lights on in the {room}.",
    ],
    ("light", "turn_off"): [
        "Turning off the {room} lights.",
        "Lights off in the {room}.",
        "The {room} lights are off.",
    ],
    ("light", "dim"): [
        "Dimming the {room} lights.",
        "Lowering the {room} lights.",
    ],
    ("light", "brighten"): [
        "Brightening the {room} lights.",
        "Raising the {room} lights.",
    ],
    ("light", "set_brightness"): [
        "Setting the {room} lights to {value} percent.",
        "Done — {room} lights at {value} percent.",
    ],

    ("switch", "turn_on"): [
        "Switching on the {room} outlet.",
        "Power on in the {room}.",
    ],
    ("switch", "turn_off"): [
        "Switching off the {room} outlet.",
        "Power off in the {room}.",
    ],

    ("fan", "turn_on"):  ["Turning on the {room} fan."],
    ("fan", "turn_off"): ["Turning off the {room} fan."],

    ("thermostat", "set_temperature"): [
        "Setting the {room} temperature to {value} degrees.",
        "The {room} thermostat is now set to {value} degrees.",
    ],
    ("thermostat", "increase_temp"): [
        "Raising the {room} temperature.",
        "Turning up the heat in the {room}.",
    ],
    ("thermostat", "decrease_temp"): [
        "Lowering the {room} temperature.",
        "Turning down the heat in the {room}.",
    ],

    ("lock", "lock"): [
        "The {room} door is now locked.",
        "Locking the {room} door.",
    ],
    ("lock", "unlock"): [
        "The {room} door is now unlocked.",
        "Unlocking the {room} door.",
    ],

    ("cover", "open"): [
        "Opening the {room} blinds.",
        "Raising the {room} shades.",
    ],
    ("cover", "close"): [
        "Closing the {room} blinds.",
        "Lowering the {room} shades.",
    ],

    ("media", "play"):        ["Playing in the {room}."],
    ("media", "pause"):       ["Paused in the {room}."],
    ("media", "mute"):        ["Muting the {room}."],
    ("media", "volume_up"):   ["Turning up the volume in the {room}."],
    ("media", "volume_down"): ["Turning down the volume in the {room}."],
}

GARAGE_DOOR_RESPONSES = {
    "open":  "Opening the garage door.",
    "close": "Closing the garage door.",
}

FAILURE_RESPONSES = [
    "Sorry, I couldn't do that right now.",
    "Something went wrong. Please try again.",
    "I wasn't able to reach the device.",
]

UNCLEAR_RESPONSES = [
    "Sorry, I didn't quite catch what you wanted.",
    "I'm not sure what you'd like me to do.",
    "Could you try rephrasing that?",
]


class ResponseEngine:
    def generate(self, intent: dict, success: bool) -> str:
        device = intent.get("device")
        action = intent.get("action")
        room = intent.get("room", "").replace("_", " ")

        if not device or not action:
            message = random.choice(UNCLEAR_RESPONSES)
        elif not success:
            message = random.choice(FAILURE_RESPONSES)
        else:
            templates = RESPONSES.get((device, action))
            if device == "cover" and "garage door" in intent.get("raw", "") and action in GARAGE_DOOR_RESPONSES:
                templates = [GARAGE_DOOR_RESPONSES[action]]
            if templates:
                value = self._extract_value(action, intent.get("parameters", {}))
                message = random.choice(templates).format(room=room, value=value)
            else:
                message = f"Done — {action.replace('_', ' ')} in the {room}."

        logger.info(f"Response: {message}")
        return message

    @staticmethod
    def _extract_value(action: str, params: dict) -> str:
        if action == "set_brightness":
            return str(params.get("brightness", ""))
        if action == "set_temperature":
            return str(params.get("temperature", ""))
        return params.get("color", "")
