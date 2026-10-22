import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from config import COLORS_RGB, ROOMS
from context_manager import ContextManager
from number_words import ONES, TEENS, TENS, WORD_TO_NUM

logger = logging.getLogger(__name__)

ROOM_ALIASES = {alias: room for room, aliases in ROOMS.items() for alias in aliases}
ALIASES_LONGEST_FIRST = sorted(ROOM_ALIASES, key=len, reverse=True)

COLOR_NAMES_LONGEST_FIRST = sorted(COLORS_RGB, key=len, reverse=True)
COLOR_WORDS = {word for name in COLORS_RGB for word in name.split()}
_COLOR_PATTERN = "|".join(sorted(COLOR_WORDS))

def _whole_word_pattern(words) -> re.Pattern:
    alternatives = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})\b")


REFERENCE_PATTERN = _whole_word_pattern([
    "it", "them", "those", "that",
    "the lights", "the switch", "the fan", "the thermostat", "the lock",
])

# Matched as whole words, so "ac" doesn't fire inside "back". Every spoken form has to
# be listed ("switch" and "switches"). The first device with a match wins, which is
# why "switch off the couch light" is a light.
DEVICE_KEYWORDS: dict[str, list[str]] = {
    "light":      ["light", "lights", "lamp", "lamps", "bulb", "bulbs", "overhead"],
    "switch":     ["switch", "switches", "outlet", "outlets", "plug", "plugs",
                   "socket", "sockets", "power"],
    "fan":        ["fan", "fans", "ceiling fan"],
    "thermostat": ["thermostat", "heat", "heating", "ac", "air conditioning",
                   "temperature", "temp", "cool", "cooler", "cooling",
                   "warmer", "hotter", "colder"],
    "lock":       ["lock", "unlock", "door lock", "deadbolt"],
    "cover":      ["blind", "blinds", "shade", "shades", "curtain", "curtains",
                   "shutter", "shutters", "garage door"],
    "media":      ["tv", "television", "speaker", "stereo", "volume"],
}
DEVICE_PATTERNS = {device: _whole_word_pattern(words) for device, words in DEVICE_KEYWORDS.items()}

# "switch" and "power" are also verbs; "switch on the fan" shouldn't target a switch.
VERB_USE_PATTERN = re.compile(r"\b(?:switch|power)\s+(?:(?:it|them|that|those)\s+)?(?:on|off)\b")


@dataclass
class ActionRule:
    action: str
    phrases: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        return (
            any(phrase in text for phrase in self.phrases)
            or any(re.search(pattern, text) for pattern in self.patterns)
        )


# The first matching rule wins, so order matters wherever one rule's phrase is a
# substring of another's: "stop playing" vs "play", "start a timer" vs the bare
# "start" under turn_on, "deactivate" vs "activate", "unlock" vs "lock". Questions
# come before device commands so the "on the" in "temperature on the patio" can't
# turn one into turn_on.
ACTION_RULES: list[ActionRule] = [
    ActionRule("pause", phrases=["stop playing", "pause"]),
    ActionRule("play",  phrases=["start playing", "resume playing", "play"]),

    ActionRule("timer_set",    phrases=["set a timer", "set timer", "start a timer",
                                        "start timer", "timer for"]),
    ActionRule("timer_cancel", phrases=["cancel the timer", "cancel timer", "stop the timer",
                                        "stop timer", "cancel all timers", "clear the timer"]),
    ActionRule("timer_status", phrases=["how long", "time left", "time remaining",
                                        "how much time", "check the timer", "timer status"]),

    # The regex tolerates Vosk near-misses such as "what times is" and "what timers it".
    ActionRule("tell_time",
               phrases=["what time is it", "what's the time", "what is the time",
                        "current time", "tell me the time", "time is it"],
               patterns=[r"\bwhat\s+time(?:s|rs)?\s+(?:is|it)\b"]),

    ActionRule("weather_temperature", phrases=[
        "what's the temperature", "what is the temperature", "whats the temperature",
        "what's the temp", "what is the temp", "whats the temp",
        "current temperature", "temperature outside", "temperature right now", "temperature now",
        "how hot is it", "how cold is it", "how warm is it",
        "what will the temperature be", "what will the temp be",
        "how hot will it be", "how cold will it be", "how warm will it be",
    ]),
    ActionRule("weather_today", phrases=[
        "what's the weather", "what is the weather", "how's the weather", "hows the weather",
        "whats the weather", "how is the weather", "what will the weather be",
        "weather today", "weather forecast", "will it rain", "is it going to rain",
        "weather at", "weather tonight", "weather this",
    ]),
    ActionRule("weather_riding", phrases=[
        "good riding weather", "good motorcycle weather", "good day to ride",
        "good day for riding", "good day for a ride", "should i ride",
        "can i ride today", "riding conditions",
    ]),

    ActionRule("turn_off", phrases=["turn off", "switch off", "power off", "shut off", "lights off",
                                    "off the", "disable", "deactivate", "stop"],
               patterns=[r"\b(?:turn|switch|power|shut)\s+(?:it|them|that|those)\s+off\b"]),
    ActionRule("turn_on",  phrases=["turn on", "switch on", "power on", "lights on",
                                    "on the", "enable", "activate", "start"],
               patterns=[r"\b(?:turn|switch|power)\s+(?:it|them|that|those)\s+on\b"]),
    ActionRule("set_color", patterns=[
        rf"(?:set|change|make|turn)\b.{{0,30}}\bto\b.{{0,20}}\b(?:{_COLOR_PATTERN})",
        r"(?:set|change|make)\b.{0,20}\bcolor\b",
        r"\d{3,5}\s*(?:k\b|kelvin)",
        r"\w+\s+(?:thousand|hundred)\s*(?:k\b|kelvin)",
    ]),

    ActionRule("dim",
               phrases=["dim", "dimmer", "reduce the brightness", "decrease the brightness"],
               patterns=[r"lower the(?!\s*(?:blinds|shades|temp|volume))"]),
    ActionRule("brighten",
               phrases=["brighten", "brighter", "increase the brightness", "raise the brightness"]),
    ActionRule("set_brightness",
               patterns=[r"set.*brightness", r"brightness.*\d+", r"\d+\s*percent"]),

    ActionRule("unlock", phrases=["unlock", "unsecure", "open the lock"]),
    ActionRule("lock",   phrases=["lock", "secure"]),
    ActionRule("open",   phrases=["open", "raise the blinds", "raise the shades"]),
    ActionRule("close",  phrases=["close", "shut", "lower the blinds", "lower the shades"]),

    ActionRule("set_temperature",
               phrases=["heat to", "cool to"],
               patterns=[r"set.*temp", r"set.*to.*degree", r"set.*to.*\d+"]),
    ActionRule("increase_temp",
               phrases=["warmer", "hotter", "turn up the heat"],
               patterns=[r"increase.*temp", r"raise.*temp"]),
    ActionRule("decrease_temp",
               phrases=["cooler", "colder", "turn down the heat"],
               patterns=[r"decrease.*temp", r"lower.*temp"]),

    ActionRule("volume_up",   phrases=["volume up", "louder"],
               patterns=[r"increase.*volume", r"turn.*up"]),
    ActionRule("volume_down", phrases=["volume down", "quieter", "quiet"],
               patterns=[r"decrease.*volume", r"lower.*volume", r"turn.*down"]),
    ActionRule("mute",        phrases=["mute", "silence"]),
]

LIGHT_ONLY_ACTIONS = {"set_brightness", "dim", "brighten"}

# \b keeps "at" in "what" and "in" in "rain" from being read as the start of a city.
CITY_PATTERN = re.compile(
    r"\b(?:in|for|at)\s+([A-Za-z\s\[\]]+?)(?:\s+today|\s+tomorrow|\s+right now|\s+now|\s+outside|$)"
)

# Vosk tends to transcribe times as words ("two p m"), so digits and words are both handled.
_HOUR_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_MINUTE_WORDS = {"fifteen": 15, "thirty": 30, "forty five": 45}
_HOUR_TOKEN = "|".join([r"\d{1,2}", *sorted(_HOUR_WORDS, key=len, reverse=True)])
_MINUTE_TOKEN = r"\d{2}|fifteen|thirty|forty[\s-]five"
_MERIDIEM = r"[ap]\.?\s?m\.?"

# "2pm", "2:30 p.m.", "two thirty p m", "at 2 o'clock"
_TIME_WITH_MERIDIEM = re.compile(
    rf"\b(?:(?:at|around)\s+)?({_HOUR_TOKEN})(?:(?::|\s+)({_MINUTE_TOKEN}))?"
    rf"\s*({_MERIDIEM}|o'?\s?clock)(?![a-z])"
)
# "at 14:00", "at 2:30", "at two". The "at" is required so stray numbers aren't read as times.
_TIME_BARE = re.compile(
    rf"\b(?:at|around)\s+({_HOUR_TOKEN})(?:(?::|\s+)({_MINUTE_TOKEN}))?(?![\w:])"
)
_NOON_MIDNIGHT = re.compile(r"\b(?:at\s+)?(noon|midnight)\b")
# (?<!good\s) keeps the greeting "good morning, what's the weather" from meaning 9 AM.
_PERIOD = re.compile(r"(?<!good\s)\b(?:(this)\s+)?(morning|afternoon|evening|tonight)\b")
_PERIOD_HOURS = {"morning": 9, "afternoon": 15, "evening": 18, "tonight": 21}


def _hour_number(token: str) -> int:
    return int(token) if token.isdigit() else _HOUR_WORDS[token]


def _minute_number(token: str | None) -> int:
    if not token:
        return 0
    if token.isdigit():
        return min(int(token), 59)
    return _MINUTE_WORDS[re.sub(r"[\s-]+", " ", token)]


def _next_hour_occurrence(hour: int, now: datetime) -> int:
    base = hour % 12
    for candidate in (base, base + 12):
        if candidate >= now.hour:
            return candidate
    return base if 6 <= base <= 11 else base + 12


def _extract_time(text: str, now: datetime | None = None) -> tuple[dict | None, str]:
    now = now or datetime.now()

    day_offset = None
    if re.search(r"\btomorrow\b", text):
        day_offset = 1
    elif re.search(r"\btoday\b", text):
        day_offset = 0

    period = _PERIOD.search(text)
    if period and day_offset is None and (period.group(1) or period.group(2) == "tonight"):
        day_offset = 0

    hour = minute = None
    spans = [period.span()] if period else []

    if match := _TIME_WITH_MERIDIEM.search(text):
        hour, minute = _hour_number(match.group(1)), _minute_number(match.group(2))
        marker = match.group(3)
        if marker[0] in "ap":
            if hour <= 12:
                hour = hour % 12 + (12 if marker[0] == "p" else 0)
        else:  # o'clock
            hour = _next_hour_occurrence(hour, now)
        spans.append(match.span())
    elif match := _TIME_BARE.search(text):
        hour, minute = _hour_number(match.group(1)), _minute_number(match.group(2))
        if 1 <= hour <= 12:
            hour = _next_hour_occurrence(hour, now)
        spans.append(match.span())

    if hour is None and (match := _NOON_MIDNIGHT.search(text)):
        hour, minute = (12 if match.group(1) == "noon" else 0), 0
        spans.append(match.span())

    if hour is None and period:
        hour, minute = _PERIOD_HOURS[period.group(2)], 0

    if hour is None or hour > 23:
        return None, text

    remaining = text
    for start, end in sorted(spans, reverse=True):
        remaining = remaining[:start] + " " + remaining[end:]

    return {"hour": hour, "minute": minute, "day_offset": day_offset}, remaining.strip()


ROOM_WIDE_SIGNALS = ("all", "every", "the lights", "the lamps", "the bulbs", "the fans", "the switches")
ACTION_NOISE = (
    "turn on", "turn off", "switch on", "switch off", "power on", "power off",
    "shut off", "lights on", "lights off",
    "dim", "brighten", "set", "make", "activate", "deactivate", "turn", "to",
)
DEVICE_NOISE = {
    "lights", "light", "lamps", "lamp", "bulbs", "bulb", "fans", "fan",
    "switches", "switch", "the", "a", "an", "brightness", "set", "to",
}
NUMBER_WORDS = set(WORD_TO_NUM) | {"percent"}

# Spoken name used to rebuild a target such as "tv" + "light" -> "tv light".
DEVICE_SINGULAR = {"cover": "blind", "media": "speaker"}


def _words_to_number(text: str) -> int | None:
    digits = re.search(r"\b(\d+)\b", text)
    if digits:
        return int(digits.group(1))

    if re.search(r"\b(one\s+hundred|a\s+hundred|hundred)\b", text):
        return 100

    for tens_word, tens_value in TENS.items():
        for ones_word, ones_value in ONES.items():
            if re.search(rf"\b{tens_word}\s+{ones_word}\b", text):
                return tens_value + ones_value
        if re.search(rf"\b{tens_word}\b", text):
            return tens_value

    # Highest first so "thirteen" isn't shadowed by a shorter match.
    for word, value in sorted(WORD_TO_NUM.items(), key=lambda item: -item[1]):
        if re.search(rf"\b{word}\b", text):
            return value

    return None


_KELVIN_NUMBER_WORDS = "|".join(sorted({*ONES, *TEENS, *TENS}, key=len, reverse=True))
_KELVIN_HUNDREDS = re.compile(
    rf"\b((?:(?:{_KELVIN_NUMBER_WORDS})\s+){{1,2}})hundred\s*(?:k\b|kelvin)"
)


def _spoken_number(words: list[str]) -> int | None:
    if len(words) == 1:
        return WORD_TO_NUM.get(words[0])
    if len(words) == 2:
        first, second = words
        if first in TENS and second in ONES:
            return TENS[first] + ONES[second]
        if first in ONES and second in ONES:
            return ONES[first] * 10 + ONES[second]
    return None


def _clamp_kelvin(value: int) -> int:
    return max(1000, min(10000, value))


def _extract_kelvin(text: str) -> int | None:
    match = re.search(r"(\d{3,5})\s*(?:k\b|kelvin)", text)
    if match:
        return _clamp_kelvin(int(match.group(1)))

    match = re.search(r"(\w+)\s+thousand(?:\s+(\w+)\s+hundred)?\s*(?:k\b|kelvin)", text)
    if match:
        thousands = WORD_TO_NUM.get(match.group(1), 0) * 1000
        hundreds = WORD_TO_NUM.get(match.group(2), 0) * 100 if match.group(2) else 0
        if thousands + hundreds:
            return _clamp_kelvin(thousands + hundreds)

    match = _KELVIN_HUNDREDS.search(text)
    if match:
        hundreds = _spoken_number(match.group(1).split())
        if hundreds:
            return _clamp_kelvin(hundreds * 100)

    return None


class IntentEngine:

    def __init__(self, context: ContextManager):
        self.context = context

    def parse(self, text: str, room: str) -> dict:
        text = text.lower().strip().replace("per cent", "percent")
        canonical_room = self._resolve_room(text, room)

        device = self._detect_device(text) or self._resolve_from_context(text, canonical_room)
        target_name = self._extract_target_name(text, device)
        action = self._detect_action(text)

        if action == "set_color":
            action = "turn_on"

        # Only lights dim, so the device can be implied. turn_on/turn_off apply to
        # every device type and must not silently default to lights.
        if device is None and action in LIGHT_ONLY_ACTIONS:
            device = "light"

        intent = {
            "room":        canonical_room,
            "device":      device,
            "action":      action,
            "target_name": target_name,
            "parameters":  self._extract_parameters(text, action),
            "raw":         text,
            "confidence":  self._score_confidence(device, action),
        }

        if device or action:
            self.context.update(canonical_room, intent)

        logger.info(
            f"Intent parsed for '{canonical_room}': device={device}, action={action}, "
            f"target={target_name}, params={intent['parameters']}, "
            f"confidence={intent['confidence']}"
        )
        return intent

    def _detect_device(self, text: str) -> str | None:
        text = VERB_USE_PATTERN.sub(" ", text)
        for device, pattern in DEVICE_PATTERNS.items():
            if pattern.search(text):
                return device
        return None

    def _detect_action(self, text: str) -> str | None:
        for rule in ACTION_RULES:
            if rule.matches(text):
                return rule.action
        return None

    def _resolve_from_context(self, text: str, room: str) -> str | None:
        if not REFERENCE_PATTERN.search(text):
            return None
        prior = self.context.get(room)
        return prior.get("device") if prior else None

    def _resolve_room(self, text: str, default_room: str) -> str:
        for alias in ALIASES_LONGEST_FIRST:
            if alias in text:
                return ROOM_ALIASES[alias]
        return ROOM_ALIASES.get(default_room, default_room)

    def _extract_target_name(self, text: str, device_type: str | None) -> str | None:
        if device_type is None:
            return None

        if any(signal in text for signal in ROOM_WIDE_SIGNALS):
            return None

        cleaned = text
        for noise in ACTION_NOISE:
            cleaned = re.sub(rf"\b{re.escape(noise)}\b", " ", cleaned)
        for alias in ALIASES_LONGEST_FIRST:
            cleaned = re.sub(rf"\b{re.escape(alias)}\b", " ", cleaned)

        modifiers = [
            word for word in cleaned.split()
            if word not in DEVICE_NOISE
            and word not in COLOR_WORDS
            and word not in NUMBER_WORDS
            and not word.isdigit()
            and len(word) > 1
        ]
        if not modifiers:
            return None

        singular = DEVICE_SINGULAR.get(device_type, device_type)
        candidate = " ".join(modifiers) + " " + singular
        logger.debug(f"Target name candidate: '{candidate}' from text: '{text}'")
        return candidate

    def _extract_parameters(self, text: str, action: str | None) -> dict:
        params = {}

        if action in ("set_brightness", "dim", "brighten"):
            value = _words_to_number(text)
            if value is not None:
                params["brightness"] = max(0, min(100, value))

        if action == "set_temperature":
            match = re.search(r"(\d+)\s*(?:degree|degrees|°)?", text)
            if match:
                params["temperature"] = int(match.group(1))

        if action in ("weather_today", "weather_riding", "weather_temperature"):
            when, city_text = _extract_time(text)
            if when:
                params["when"] = when

            match = CITY_PATTERN.search(city_text)
            if match:
                city = match.group(1).strip()
                if city.lower() not in ("the", "a", "my", "here"):
                    params["city"] = city

        kelvin = _extract_kelvin(text)
        if kelvin is not None:
            params["color_temp_kelvin"] = kelvin
        else:
            for color in COLOR_NAMES_LONGEST_FIRST:
                if color in text:
                    params["color"] = color
                    break

        return params

    @staticmethod
    def _score_confidence(device: str | None, action: str | None) -> str:
        if device and action:
            return "high"
        if device or action:
            return "medium"
        return "low"