import logging
import re
import threading
import time

import paho.mqtt.client as mqtt

from config import MQTT_BROKER, MQTT_PORT, SPEECH_TOPIC, TIMER_SOUND_TOPIC
from number_words import ONES, TENS, WORD_TO_NUM

logger = logging.getLogger(__name__)

AUTO_NAME_PREFIX = "timer "

DURATION_UNITS = (
    (3600, ["hour", "hours", "hr", "hrs"]),
    (60,   ["minute", "minutes", "min", "mins"]),
    (1,    ["second", "seconds", "sec", "secs"]),
)

QUANTITY_WORDS = {"a": 1, **WORD_TO_NUM}


class Timer:
    def __init__(self, name: str, duration_sec: float, room: str, on_fire):
        self.name = name
        self.duration_sec = duration_sec
        self.room = room
        self.started_at = time.time()
        self._cancel_event = threading.Event()
        threading.Thread(target=self._run, args=(on_fire,), daemon=True).start()

    def _run(self, on_fire) -> None:
        cancelled = self._cancel_event.wait(self.duration_sec)
        if not cancelled:
            on_fire(self)

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def remaining_sec(self) -> float:
        return max(0.0, self.duration_sec - (time.time() - self.started_at))

    @property
    def is_done(self) -> bool:
        return self.remaining_sec <= 0 or self.cancelled


class TimerEngine:
    def __init__(self):
        self._timers: dict[str, Timer] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iris_timer_engine")

        # Connect in the background with backoff. If the broker isn't up yet at boot, a
        # single failed attempt would leave every timer unable to alert, with no other symptom.
        threading.Thread(target=self._connect_with_retry, daemon=True).start()

    def set_timer(self, text: str, room: str) -> str:
        duration_sec, name = _parse_timer_request(text)

        if duration_sec is None:
            return "Sorry, I didn't catch the duration. Try saying something like: set a timer for 5 minutes."
        if duration_sec <= 0:
            return "The timer duration needs to be greater than zero."

        with self._lock:
            if not name:
                self._counter += 1
                name = f"{AUTO_NAME_PREFIX}{self._counter}"
            if name in self._timers and not self._timers[name].is_done:
                return f"There's already a {name} running. Cancel it first or give this one a different name."

        timer = Timer(name, duration_sec, room, self._on_fire)
        with self._lock:
            self._timers[name] = timer

        confirmation = f"Timer set for {_seconds_to_speech(duration_sec)}."
        if not _is_auto_named(name):
            confirmation += f" I'll call it '{name}'."
        return confirmation

    def cancel_timer(self, text: str) -> str:
        if "all" in text.lower():
            with self._lock:
                active = [t for t in self._timers.values() if not t.is_done]
                for timer in active:
                    timer.cancel()
            if not active:
                return "There are no active timers to cancel."
            return f"Cancelled {len(active)} timer{'s' if len(active) > 1 else ''}."

        name = self._extract_timer_name(text)
        with self._lock:
            timer = self._timers.get(name)
        if timer is not None and not timer.is_done:
            timer.cancel()
            return f"Timer '{name}' cancelled."

        with self._lock:
            matches = [t for n, t in self._timers.items() if name in n and not t.is_done]
        if len(matches) == 1:
            matches[0].cancel()
            return "Timer cancelled."
        if len(matches) > 1:
            return f"I found multiple timers matching '{name}'. Be more specific or say 'cancel all timers'."
        return f"I couldn't find an active timer called '{name}'."

    def timer_status(self) -> str:
        with self._lock:
            active = {name: timer for name, timer in self._timers.items() if not timer.is_done}

        if not active:
            return "There are no active timers right now."

        if len(active) == 1:
            name, timer = next(iter(active.items()))
            label = "your timer" if _is_auto_named(name) else f"the {name}"
            return f"There's {_seconds_to_speech(timer.remaining_sec)} left on {label}."

        parts = [f"You have {len(active)} active timers:"]
        for name, timer in sorted(active.items()):
            parts.append(f"{name}: {_seconds_to_speech(timer.remaining_sec)} remaining.")
        return " ".join(parts)

    def _connect_with_retry(self) -> None:
        delay = 1
        while True:
            try:
                self._mqtt.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
                self._mqtt.loop_start()
                logger.info("TimerEngine: connected to MQTT broker.")
                return
            except Exception as e:
                logger.warning(f"TimerEngine: could not connect to MQTT broker ({e}). Retrying in {delay}s …")
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def _on_fire(self, timer: Timer) -> None:
        if timer.cancelled:
            return

        if _is_auto_named(timer.name):
            message = "Timer's up!"
        else:
            message = f"{timer.name.capitalize()} is done!"
        logger.info(f"Timer fired: '{timer.name}' in room '{timer.room}'")

        try:
            # The satellite queues the spoken message behind the alarm sound.
            self._mqtt.publish(f"{TIMER_SOUND_TOPIC}/{timer.room}", "play", qos=1)
            self._mqtt.publish(f"{SPEECH_TOPIC}/{timer.room}", message, qos=1)
        except Exception as e:
            logger.error(f"TimerEngine: could not publish timer alert: {e}")

        with self._lock:
            self._timers.pop(timer.name, None)

    def _extract_timer_name(self, text: str) -> str:
        text = text.lower()

        match = re.search(r"(?:cancel|stop|clear)\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+timer", text)
        if match and match.group(1).strip() not in ("a", "the", "an", "all"):
            return match.group(1).strip()

        match = re.search(r"(?:on|for)\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s+timer", text)
        if match:
            return match.group(1).strip()

        with self._lock:
            for name, timer in self._timers.items():
                if not timer.is_done:
                    return name
        return f"{AUTO_NAME_PREFIX}1"


def _is_auto_named(name: str) -> bool:
    return name.startswith(AUTO_NAME_PREFIX)


def _parse_timer_request(text: str) -> tuple[float | None, str | None]:
    """Returns (duration in seconds, optional timer name); duration is None if none was heard."""
    text = text.lower()

    name = None
    match = re.search(r"set\s+(?:a\s+)?(\w+(?:\s+\w+)?)\s+timer\s+for", text)
    if match and match.group(1).strip() not in ("a", "the", "an"):
        name = match.group(1).strip()

    total_sec = 0.0
    found = False
    for seconds_per_unit, unit_words in DURATION_UNITS:
        quantity = _extract_quantity(text, unit_words)
        if quantity is not None:
            total_sec += quantity * seconds_per_unit
            found = True

    return (total_sec if found else None), name


def _extract_quantity(text: str, unit_words: list[str]) -> float | None:
    units = "|".join(unit_words)

    match = re.search(rf"(\d+(?:\.\d+)?)\s+(?:{units})", text)
    if match:
        return float(match.group(1))

    number_words = "|".join(QUANTITY_WORDS)
    for fraction_word, fraction in (("half", 0.5), ("quarter", 0.25)):
        match = re.search(rf"({number_words})\s+and\s+a\s+{fraction_word}\s+(?:{units})", text)
        if match:
            return QUANTITY_WORDS.get(match.group(1), 0) + fraction

    # Must come before the single-word loop, which would read "half a minute" as "a minute".
    if re.search(rf"half\s+(?:an?\s+)?(?:{units})", text):
        return 0.5

    for tens_word, tens_value in TENS.items():
        for ones_word, ones_value in ONES.items():
            if re.search(rf"{tens_word}\s+{ones_word}\s+(?:{units})", text):
                return float(tens_value + ones_value)

    for word, value in sorted(QUANTITY_WORDS.items(), key=lambda item: -item[1]):
        if re.search(rf"\b{word}\s+(?:{units})", text):
            return float(value)

    return None


def _seconds_to_speech(sec: float) -> str:
    sec = int(round(sec))
    hours = sec // 3600
    mins = (sec % 3600) // 60
    secs = sec % 60

    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")

    if not parts:
        return "less than a second"
    return " and ".join(parts[:2])
