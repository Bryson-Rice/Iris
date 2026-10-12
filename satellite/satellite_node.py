import contextlib
import ctypes
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
import vosk

try:
    import samplerate
except ImportError:
    samplerate = None

# node-setup.sh rewrites the assignments for these names, so keep each on one line.
ROOM_NAME = "bedroom"
MQTT_BROKER = "10.0.0.142"
MODEL_PATH = "model"
PIPER_BINARY = "/usr/local/bin/piper"
PIPER_MODEL = os.path.expanduser("~/piper/voice.onnx")

MQTT_PORT = 1883
VOICE_TOPIC = f"iris/voice/{ROOM_NAME}"
RESPONSE_TOPIC = f"iris/speech/{ROOM_NAME}"
TIMER_SOUND_TOPIC = f"iris/timer_sound/{ROOM_NAME}"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WAKE_SOUND = os.path.join(BASE_DIR, "wake.wav")
TIMER_SOUND = os.path.join(BASE_DIR, "timer.wav")

WAKE_WORD = "iris"
COMMAND_TIMEOUT_SEC = 6

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_SIZE = 8000
FALLBACK_RATES = [16000, 44100, 48000, 32000, 22050, 8000]

# Audio arrives in half-second blocks, so this long without one means the stream died.
AUDIO_STALL_SEC = 4
AUDIO_POLL_SEC = 1
AUDIO_RETRY_SEC = 3
# Once the microphone has worked, reconnecting that makes no progress for this long means
# something is hung, so the process exits and systemd restarts it.
AUDIO_STUCK_SEC = 60
ASOUND_CONF = "/etc/asound.conf"

PIPER_USE_PIPER = os.path.isfile(PIPER_BINARY) and os.path.isfile(PIPER_MODEL)


def _piper_sample_rate() -> int:
    # Low-quality voices are 16 kHz and the rest 22.05 kHz; playing at the wrong rate
    # makes speech too fast or too slow.
    try:
        with open(f"{PIPER_MODEL}.json") as voice_config:
            return int(json.load(voice_config)["audio"]["sample_rate"])
    except (OSError, KeyError, TypeError, ValueError):
        return 22050


PIPER_SAMPLE_RATE = _piper_sample_rate()

# Commands are recognised against this closed vocabulary. A word that isn't listed
# comes back as [unk], so every word the server's intent_engine, scene_engine and
# timer_engine rely on has to appear here. Vosk builds its language model from each
# entry as a sentence, so full phrases give it word order to lean on. Single words alone
# leave it free to pick sound-alikes ("what timers it").
WAKE_WORDS = ["iris"]

ACTION_WORDS = [
    "turn on", "turn off", "switch on", "switch off",
    "dim", "brighten", "dimmer", "brighter", "set", "make", "change",
    "enable", "disable", "activate", "deactivate", "power",
    "start", "stop", "cancel", "clear", "check",
    "lock", "unlock", "secure", "unsecure",
    "open", "close", "shut", "raise", "lower",
    "increase", "decrease", "reduce",
    "play", "playing", "pause", "resume",
    "mute", "silence", "louder", "quieter", "quiet",
    "color",
]

DEVICE_WORDS = [
    "light", "lights", "lamp", "lamps", "bulb", "bulbs",
    "fan", "switch", "outlet", "plug", "socket",
    "thermostat", "heat", "heating", "cooling", "air conditioning", "ac",
    "deadbolt", "door",
    "blind", "blinds", "shade", "shades", "curtain", "curtains", "shutter", "shutters",
    "tv", "television", "speaker", "stereo", "volume",
]

ROOM_WORDS = [
    "bedroom", "master", "kids", "guest", "living room", "lounge", "kitchen", "bathroom", "bath",
    "dining room", "hallway", "hall", "office", "garage",
    "basement", "attic", "patio", "backyard", "back", "yard", "front",
]

FILLER_WORDS = [
    "the", "a", "an", "all", "couch", "ceiling", "overhead",
    "on", "off", "up", "down", "to", "for", "and", "be", "i", "is", "it", "them",
]

NUMBER_WORDS = [
    "percent", "brightness",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
    "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "half", "quarter",
]

COLOR_WORDS = [
    "red", "green", "blue", "white", "yellow",
    "orange", "purple", "pink", "cyan",
    "warm white", "cool white",
]

COLOR_TEMPERATURE_WORDS = [
    "kelvin", "k",
    "twenty five hundred", "twenty seven hundred", "thirty five hundred", "forty five hundred",
    "sixty five hundred", "six five hundred",
    "three thousand", "four thousand", "five thousand", "six thousand",
]

TEMPERATURE_WORDS = [
    "degrees", "degree", "temperature", "temp", "warmer", "cooler", "hotter", "colder",
]

# Named timers ("pasta timer") only work if the name is added here too.
TIMER_WORDS = [
    "timer", "timers", "how", "long", "left", "remaining", "much", "status",
    "hour", "hours", "hr", "hrs", "minute", "minutes", "min", "mins",
    "second", "seconds", "sec", "secs",
]

WEATHER_WORDS = [
    "weather", "today", "forecast", "will", "rain", "how's", "hows", "what's", "whats",
    "riding", "ride", "conditions", "motorcycle", "day", "should", "can",
    "outside", "hot", "cold", "warm", "right", "now",
]

TIME_WORDS = ["what", "current", "tell", "me", "time"]

SCENE_WORDS = [
    "good", "night", "goodnight", "sleep", "going", "bed", "bedtime",
    "cozy", "mode", "morning", "wake", "rise", "shine", "movie", "watching", "film",
]

MISC_WORDS = ["please", "hey"]

PHRASES = [
    "what time is it", "what is the time", "what's the time", "whats the time",
    "what's the current time", "what is the current time", "tell me the time",
    "tell me what time it is", "current time", "time is it",
    "how much time is left", "how much time is left on the timer", "how long is left",
    "time left", "time remaining", "check the timer", "timer status",
    "cancel the timer", "cancel all timers", "stop the timer",
    "set a timer for", "start a timer for",
    "what's the weather", "what is the weather", "how's the weather",
    "what's the weather today", "will it rain today", "is it going to rain",
    "what's the temperature", "what is the temperature", "whats the temperature",
    "what's the temp", "what is the temp", "current temperature",
    "what's the temperature outside", "what is the temperature outside",
    "temperature outside", "temperature right now",
    "how hot is it", "how cold is it", "how warm is it",
    "how hot is it outside", "how cold is it outside",
    "turn it on", "turn it off", "turn them on", "turn them off",
    "open the garage door", "close the garage door",
]

# Open-Meteo resolves a spoken name to its most populous match, so names where that
# is the wrong city for most people (Athens, Birmingham, Columbus) are left out.
CITY_NAMES = [
    "new york", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
    "san antonio", "san diego", "dallas", "san jose", "austin", "jacksonville",
    "fort worth", "charlotte", "san francisco", "indianapolis", "seattle", "denver",
    "boston", "el paso", "nashville", "detroit", "oklahoma city", "portland",
    "las vegas", "memphis", "louisville", "baltimore", "milwaukee", "albuquerque",
    "tucson", "sacramento", "kansas city", "omaha", "raleigh", "miami", "minneapolis",
    "tulsa", "cleveland", "new orleans", "tampa", "orlando", "pittsburgh", "cincinnati",
    "salt lake city", "honolulu", "anchorage", "atlanta", "augusta", "savannah", "macon",
    "asheville", "greenville", "charleston", "knoxville", "chattanooga", "montgomery",
    "buffalo", "boise",
    "london", "paris", "tokyo", "toronto", "vancouver", "mexico city", "sydney",
    "berlin", "rome", "madrid", "dublin", "amsterdam",
]

# Cities are listed as "in <city>" so the language model only expects one after "in"
# instead of anywhere in a command.
CITY_LEAD_INS = [
    "what's the weather in", "what is the weather in", "how's the weather in", "whats the weather in",
    "what's the temperature in", "what is the temperature in",
    "how hot is it in", "how cold is it in", "how warm is it in",
    "will it rain in", "is it going to rain in", "good day to ride in",
]
CITY_PHRASES = [f"in {city}" for city in CITY_NAMES]

COMMAND_GRAMMAR = [
    *WAKE_WORDS, *ACTION_WORDS, *DEVICE_WORDS, *ROOM_WORDS, *FILLER_WORDS,
    *NUMBER_WORDS, *COLOR_WORDS, *COLOR_TEMPERATURE_WORDS, *TEMPERATURE_WORDS,
    *TIMER_WORDS, *WEATHER_WORDS, *TIME_WORDS, *SCENE_WORDS, *MISC_WORDS, *PHRASES,
    *CITY_LEAD_INS, *CITY_PHRASES,
    "[unk]",
]

_audio_queue: queue.Queue = queue.Queue()
_mqtt_client: mqtt.Client | None = None
_running = True
_audio_restart = threading.Event()
_stream_open = threading.Event()
_recovery_armed = threading.Event()
_last_audio_time = time.monotonic()
_last_progress = time.monotonic()

# _audio_lock serialises all audio output (alarm, TTS, wake chime) so nothing overlaps.
_audio_lock = threading.Lock()
_alarm_proc: subprocess.Popen | None = None
_alarm_proc_lock = threading.Lock()
_alarm_stop = threading.Event()


def _filter_grammar(model, grammar: list[str]) -> tuple[list[str], list[str]]:
    # Splits the grammar into (usable, dropped). Vosk warns on every recognizer build
    # about each word the model lacks, and keeps the rest of a phrase, which would leave
    # fragments like "in" on its own; dropping the whole entry is cleaner
    find_word = getattr(model, "vosk_model_find_word", None)
    if find_word is None or find_word("the") == -1:
        return grammar, []

    usable, dropped = [], []
    for entry in grammar:
        if entry == "[unk]" or all(find_word(word) != -1 for word in entry.split()):
            usable.append(entry)
        else:
            dropped.append(entry)
    return usable, dropped


def _silence_alsa_errors() -> None:
    try:
        asound = ctypes.cdll.LoadLibrary("libasound.so.2")
        asound.snd_lib_error_set_handler(ctypes.c_void_p(None))
    except Exception:
        pass


@contextlib.contextmanager
def _stderr_silenced():
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved_stderr = os.dup(2)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(saved_stderr, 2)
        os.close(saved_stderr)
        os.close(devnull)


def _can_open_stream(rate: int) -> bool:
    # Actually opening a stream is more reliable than check_input_settings(), which
    # some drivers pass for rates they then fail to open. PortAudio/ALSA log noisily while probing.
    try:
        with _stderr_silenced(), sd.RawInputStream(
            samplerate=rate, channels=CHANNELS, dtype="int16", blocksize=256
        ):
            return True
    except Exception:
        return False


def get_device_sample_rate() -> int | None:
    # The first sample rate the default microphone opens at or None if there isn't one
    _silence_alsa_errors()

    try:
        device_info = sd.query_devices(kind="input")
    except Exception:
        return None
    device_name = device_info.get("name", "unknown")
    default_rate = int(device_info["default_samplerate"])

    candidates = sorted(
        set(FALLBACK_RATES + [default_rate]),
        key=lambda r: (r != SAMPLE_RATE, r != default_rate, abs(r - SAMPLE_RATE)),
    )

    for rate in candidates:
        if not _can_open_stream(rate):
            continue
        if rate == SAMPLE_RATE:
            print(f"[audio] Sample rate: {rate} Hz on '{device_name}'.", flush=True)
        else:
            print(
                f"[audio] '{device_name}' doesn't support {SAMPLE_RATE} Hz. "
                f"Using {rate} Hz (will resample for Vosk).",
                flush=True,
            )
        return rate

    return None


def _kill_alarm() -> None:
    global _alarm_proc
    _alarm_stop.set()
    with _alarm_proc_lock:
        if _alarm_proc is not None:
            try:
                _alarm_proc.kill()
                _alarm_proc.wait(timeout=1)
            except Exception:
                pass
            _alarm_proc = None


def _audio_callback(indata, frames, time_info, status):
    global _last_audio_time
    _last_audio_time = time.monotonic()

    # Overflow while idling on the wake word is normal, so don't report it.
    if status and "overflow" not in str(status).lower():
        print(f"[audio] {status}", file=sys.stderr)
    _audio_queue.put(bytes(indata))


def _drain_audio_queue() -> None:
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except Exception:
            break


class SpeechRecognizer:
    def __init__(self, device_rate: int):
        if not os.path.isdir(MODEL_PATH):
            print(
                f"[ERROR] Vosk model not found at '{MODEL_PATH}'.\n"
                "Download from https://alphacephei.com/vosk/models and unzip here.",
                file=sys.stderr,
            )
            sys.exit(1)

        print("Loading Vosk speech model …", flush=True)
        self.model = vosk.Model(MODEL_PATH)

        grammar, dropped = _filter_grammar(self.model, COMMAND_GRAMMAR)
        self._grammar_json = json.dumps(grammar)
        if dropped:
            print(f"[grammar] Not in the speech model's vocabulary, left out: {', '.join(dropped)}", flush=True)

        self.set_input_rate(device_rate)

    def set_input_rate(self, device_rate: int) -> None:
        self.device_rate = device_rate
        self.needs_resamp = device_rate != SAMPLE_RATE

        if self.needs_resamp:
            if samplerate is not None:
                print(f"[audio] Resampling {device_rate} Hz → {SAMPLE_RATE} Hz via samplerate lib", flush=True)
            else:
                print(
                    f"[audio] samplerate lib not found; using basic decimation "
                    f"({device_rate} Hz → {SAMPLE_RATE} Hz). Install with: pip install samplerate",
                    flush=True,
                )

        self._new_wake_recognizer()
        self.new_command_recognizer()

    def _new_wake_recognizer(self) -> None:
        self._wake_rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        self._wake_rec.SetWords(False)

    def new_command_recognizer(self) -> None:
        self._cmd_rec = vosk.KaldiRecognizer(self.model, SAMPLE_RATE)
        self._cmd_rec.SetGrammar(self._grammar_json)
        self._cmd_rec.SetWords(False)

    def listen_for_wake_word(self) -> bool:
        # Returns True once the wake word is heard False on shutdown or a device change
        while _running and not _audio_restart.is_set():
            try:
                data = _audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if self.needs_resamp:
                data = self._resample(data)

            # Checking partial results lets the wake word fire mid-utterance. Calling
            # Result() on every chunk instead force-finalises the decoder before "iris"
            # has enough audio to be recognised.
            if self._wake_rec.AcceptWaveform(data):
                heard = json.loads(self._wake_rec.Result()).get("text", "")
            else:
                heard = json.loads(self._wake_rec.PartialResult()).get("partial", "")

            if WAKE_WORD in heard.lower():
                self._new_wake_recognizer()
                _kill_alarm()
                return True

        return False

    def listen_for_phrase(self, timeout_sec: float) -> str | None:
        deadline = time.time() + timeout_sec

        while _running and not _audio_restart.is_set():
            if time.time() > deadline:
                text = json.loads(self._cmd_rec.FinalResult()).get("text", "")
                self.new_command_recognizer()
                return text or None

            try:
                data = _audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if self.needs_resamp:
                data = self._resample(data)

            if self._cmd_rec.AcceptWaveform(data):
                text = json.loads(self._cmd_rec.Result()).get("text", "").strip()
                self.new_command_recognizer()
                if text:
                    return text

        return None

    def _resample(self, data: bytes) -> bytes:
        samples = np.frombuffer(data, dtype=np.int16)

        if samplerate is not None:
            floats = samples.astype(np.float32) / 32768.0
            resampled = samplerate.resample(floats, SAMPLE_RATE / self.device_rate, "sinc_best")
            return (resampled * 32768.0).astype(np.int16).tobytes()

        # Crude fallback: keep every Nth sample. Adequate for 44.1/48 kHz down to 16 kHz.
        indices = np.arange(0, len(samples), self.device_rate / SAMPLE_RATE).astype(int)
        return samples[indices[indices < len(samples)]].tobytes()


def wait_for_wake_word(recognizer: SpeechRecognizer) -> bool:
    print(f"[{ROOM_NAME}] Waiting for wake word '{WAKE_WORD}' …", flush=True)
    detected = recognizer.listen_for_wake_word()
    if detected:
        print(f"[{ROOM_NAME}] Wake word detected.", flush=True)
    return detected


def record_command(recognizer: SpeechRecognizer) -> str | None:
    print(f"[{ROOM_NAME}] Listening for command …", flush=True)
    _play_activation_sound()

    text = recognizer.listen_for_phrase(COMMAND_TIMEOUT_SEC)
    if text:
        print(f"[{ROOM_NAME}] Command recognised: '{text}'", flush=True)
    else:
        print(f"[{ROOM_NAME}] No command heard.", flush=True)
    return text


def speak(text: str) -> None:
    print(f"[{ROOM_NAME}] Speaking: '{text}'", flush=True)
    with _audio_lock:
        if PIPER_USE_PIPER:
            _speak_piper(text)
        else:
            _speak_espeak(text)


def _speak_piper(text: str) -> None:
    try:
        piper_proc = subprocess.Popen(
            [PIPER_BINARY, "--model", PIPER_MODEL, "--output-raw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        aplay_proc = subprocess.Popen(
            ["aplay", "-r", str(PIPER_SAMPLE_RATE), "-f", "S16_LE", "-t", "raw", "-"],
            stdin=piper_proc.stdout,
            stderr=subprocess.DEVNULL,
        )
        piper_proc.stdin.write(text.encode())
        piper_proc.stdin.close()
        aplay_proc.wait()
        piper_proc.wait()
    except Exception as e:
        print(f"[TTS] Piper failed ({e}); falling back to espeak.", file=sys.stderr)
        _speak_espeak(text)


def _speak_espeak(text: str) -> None:
    subprocess.run(["espeak-ng", text], stderr=subprocess.DEVNULL, check=False)


def _play_activation_sound() -> None:
    with _audio_lock:
        if os.path.isfile(WAKE_SOUND):
            subprocess.run(["aplay", "-q", WAKE_SOUND], stderr=subprocess.DEVNULL)
        else:
            print(f"[wake] wake.wav not found at '{WAKE_SOUND}', using espeak fallback.", flush=True)
            subprocess.run(["espeak-ng", "ready"], stderr=subprocess.DEVNULL, check=False)

    # Drop whatever the mic picked up from the chime so it isn't recognised as a command.
    _drain_audio_queue()
    time.sleep(0.05)
    _drain_audio_queue()


def _play_timer_sound() -> None:
    # Three alarm rings, abortable mid-play by _kill_alarm() when the wake word is heard
    global _alarm_proc
    _alarm_stop.clear()

    with _audio_lock:
        if os.path.isfile(TIMER_SOUND):
            for _ in range(3):
                if _alarm_stop.is_set():
                    break
                with _alarm_proc_lock:
                    proc = subprocess.Popen(["aplay", "-q", TIMER_SOUND], stderr=subprocess.DEVNULL)
                    _alarm_proc = proc
                proc.wait()
                with _alarm_proc_lock:
                    _alarm_proc = None
                if _alarm_stop.is_set():
                    break
                time.sleep(0.3)
        else:
            print(f"[timer] timer.wav not found at '{TIMER_SOUND}', using espeak fallback.", flush=True)
            for _ in range(3):
                if _alarm_stop.is_set():
                    break
                subprocess.run(["espeak-ng", "beep"], stderr=subprocess.DEVNULL, check=False)
                time.sleep(0.4)


def _on_connect(client, userdata, flags, reason_code, properties=None) -> None:
    if reason_code == 0:
        print(f"[{ROOM_NAME}] Connected to MQTT broker.", flush=True)
        client.subscribe(RESPONSE_TOPIC, qos=1)
        client.subscribe(TIMER_SOUND_TOPIC, qos=1)
        print(f"[{ROOM_NAME}] Subscribed to '{RESPONSE_TOPIC}' and '{TIMER_SOUND_TOPIC}'.", flush=True)
    else:
        print(f"[{ROOM_NAME}] MQTT connection failed (reason={reason_code}).", file=sys.stderr)


def _on_disconnect(client, userdata, flags, reason_code=None, properties=None) -> None:
    if reason_code != 0:
        print(f"[{ROOM_NAME}] Unexpected MQTT disconnect (reason={reason_code}). Will retry.", flush=True)


def _on_message(client, userdata, msg: mqtt.MQTTMessage) -> None:
    text = msg.payload.decode("utf-8", errors="replace").strip()

    if msg.topic == TIMER_SOUND_TOPIC:
        threading.Thread(target=_play_timer_sound, daemon=True).start()
    elif text:
        threading.Thread(target=speak, args=(text,), daemon=True).start()


def publish_command(text: str) -> None:
    if _mqtt_client is None:
        print("[MQTT] Client not initialised.", file=sys.stderr)
        return

    payload = json.dumps({"room": ROOM_NAME, "text": text, "timestamp": time.time()})
    _mqtt_client.publish(VOICE_TOPIC, payload, qos=1)
    print(f"[{ROOM_NAME}] Published to '{VOICE_TOPIC}': {payload}", flush=True)


def _mark_progress() -> None:
    global _last_progress
    _last_progress = time.monotonic()


def _request_audio_restart(reason: str) -> None:
    print(f"[audio] {reason} Reconnecting …", flush=True)
    _mark_progress()
    _audio_restart.set()


def _watch_audio_device() -> None:
    # Notices a lost microphone. PortAudio never reports an unplugged device: its
    # stream just stops delivering audio, and its device list isn't refreshed
    while _running:
        time.sleep(AUDIO_POLL_SEC)
        now = time.monotonic()

        if _stream_open.is_set() and not _audio_restart.is_set():
            silent_for = now - _last_audio_time
            if silent_for > AUDIO_STALL_SEC:
                _request_audio_restart(f"No audio for {silent_for:.0f} s; the microphone was probably disconnected.")
        elif _recovery_armed.is_set() and now - _last_progress > AUDIO_STUCK_SEC:
            print("[audio] Reconnecting is stuck. Exiting so the service restarts.", file=sys.stderr, flush=True)
            os._exit(1)


def _reinitialize_portaudio() -> None:
    # PortAudio lists devices once, when it starts, so a device that is replugged
    # (possibly under a new card number) isn't visible until it is restarted. The
    # terminate and initialize calls are private API, but the only way to do that.
    try:
        sd._terminate()
    except Exception:
        pass
    try:
        sd._initialize()
    except Exception as e:
        print(f"[audio] Could not refresh the audio device list: {e}", file=sys.stderr)


def _sleep_while_running(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while _running and time.monotonic() < deadline:
        time.sleep(0.1)


def _wait_for_microphone() -> int | None:
    # Returns the microphone's sample rate, waiting for it to be connected if need be.
    # None means the satellite is shutting down
    waiting = False
    while _running:
        rate = get_device_sample_rate()
        if rate is not None:
            if waiting:
                print("[audio] Microphone connected.", flush=True)
            return rate

        if not waiting:
            waiting = True
            print("[audio] No microphone found. Waiting for one to be connected …", flush=True)
            print(
                "[audio] To see what is detected: "
                "python -c \"import sounddevice as sd; print(sd.query_devices())\"",
                flush=True,
            )
        _mark_progress()
        _sleep_while_running(AUDIO_RETRY_SEC)
        _reinitialize_portaudio()
    return None


def _warn_if_card_selected_by_number() -> None:
    try:
        with open(ASOUND_CONF) as config:
            selects_by_number = re.search(r"hw:\d", config.read())
    except OSError:
        return

    if selects_by_number:
        print(
            f"[audio] {ASOUND_CONF} selects the sound card by number, which can change when the "
            "USB device is replugged. Re-run node-setup.sh to select it by name instead.",
            flush=True,
        )


def _signal_handler(sig, frame) -> None:
    global _running
    print(f"\n[{ROOM_NAME}] Caught signal {sig}. Stopping …", flush=True)
    _running = False


def _listen_forever() -> None:
    # The wake word and command loop. Losing the microphone ends one pass of the outer
    # loop, and the next pass waits for it to come back
    global _last_audio_time
    recognizer: SpeechRecognizer | None = None

    while _running:
        device_rate = _wait_for_microphone()
        if device_rate is None:
            return

        _mark_progress()
        if recognizer is None:
            recognizer = SpeechRecognizer(device_rate)
        else:
            recognizer.set_input_rate(device_rate)

        _audio_restart.clear()
        _drain_audio_queue()
        block_size = int(BLOCK_SIZE * device_rate / SAMPLE_RATE)

        try:
            with sd.RawInputStream(
                samplerate=device_rate,
                blocksize=block_size,
                dtype="int16",
                channels=CHANNELS,
                callback=_audio_callback,
            ):
                _last_audio_time = time.monotonic()
                _stream_open.set()
                _recovery_armed.set()
                _mark_progress()
                print(f"[{ROOM_NAME}] Microphone open. Iris satellite running.", flush=True)

                while _running and not _audio_restart.is_set():
                    if not wait_for_wake_word(recognizer):
                        break

                    _drain_audio_queue()
                    recognizer.new_command_recognizer()

                    command = record_command(recognizer)
                    if command:
                        publish_command(command)

                    time.sleep(0.5)

        except Exception as e:
            print(f"[audio] Stream error: {e}", file=sys.stderr)
        finally:
            _stream_open.clear()
            _mark_progress()

        if _running:
            _reinitialize_portaudio()


def main() -> None:
    global _mqtt_client

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"iris_satellite_{ROOM_NAME}")
    _mqtt_client.on_connect = _on_connect
    _mqtt_client.on_disconnect = _on_disconnect
    _mqtt_client.on_message = _on_message
    _mqtt_client.reconnect_delay_set(min_delay=2, max_delay=10)

    print(f"[{ROOM_NAME}] Connecting to MQTT broker {MQTT_BROKER}:{MQTT_PORT} …")
    _mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    _mqtt_client.loop_start()

    _warn_if_card_selected_by_number()
    _mark_progress()
    threading.Thread(target=_watch_audio_device, daemon=True).start()

    _listen_forever()

    _mqtt_client.loop_stop()
    _mqtt_client.disconnect()
    print(f"[{ROOM_NAME}] Satellite shut down.", flush=True)


if __name__ == "__main__":
    main()
