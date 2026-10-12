# Iris

A self-hosted voice assistant for smart homes. Raspberry Pi satellites in each room listen for a wake word and transcribe commands locally; a central server works out what was meant and drives Home Assistant. No audio ever leaves the house.


## How a command flows

1. The satellite hears the wake word (`iris`), plays a chime, and records a command with [Vosk](https://alphacephei.com/vosk/).
2. It publishes `{"room": "bedroom", "text": "turn off the lights"}` to `iris/voice/<room>`.
3. The server checks the text against scenes, then parses it into a room, device, action and parameters.
4. The action runs against Home Assistant, timers, or the weather service.
5. The reply is published to `iris/speech/<room>` and the satellite speaks it with [Piper](https://github.com/rhasspy/piper) (falling back to espeak-ng).

Follow-ups like "dim them" or "turn it off" work because each room remembers its last device for `CONTEXT_TTL_SECONDS`.

## What it can do

| Area | Examples |
| --- | --- |
| Lights | "turn off the couch light", "dim the lights to thirty percent", "set the lights to warm white", "set the lights to twenty seven hundred kelvin" |
| Switches, fans, locks, blinds, media | "turn on the fan", "unlock the front door", "close the blinds", "close the garage door", "mute the tv" |
| Thermostat | "set the temperature to seventy two degrees", "make it warmer" |
| Scenes | "good night", "movie mode", "cozy mode", "good morning" (reads out today's date, the current temperature and conditions, and the high and low) |
| Timers | "set a pasta timer for eight minutes", "how long is left", "cancel all timers" |
| Time | "what time is it" |
| Weather | "what's the weather", "what's the temperature", "how cold is it outside", "what's the weather in chicago", "is it a good day to ride" |

* Note that Thermostat and Media is untested, it is possible they do not work correctly.

## What you need

Iris spans three devices on the same network, and the satellite needs a USB speaker and microphone. They can share hardware if you like, but this guide assumes three devices.

| | Device | Runs | Needs |
| --- | --- | --- | --- |
| 1 | **Home Assistant** | Your smart home | Any install, with your devices already set up |
| 2 | **Iris server** | `server/`, the MQTT broker, and (briefly) a file server for satellite setup | A Linux machine with Python 3.10+ |
| 3 | **Satellite(s)** | `satellite/` | A Raspberry Pi running Raspberry Pi OS |
| | **Speaker / microphone** | Plugs into the Pi | A USB speaker and microphone. I use the [EMEET M0 Plus](https://emeet.com/products/speakerphone-m0-plus), which the installer recognises automatically |

The server must be reachable from the satellites on port 1883 (MQTT), and Home Assistant from the server on its usual port (8123).

## Getting started

### 1. Home Assistant

1. Create a long-lived access token: click your user name (bottom left) → **Security** → **Long-lived access tokens** → **Create token**. Copy it somewhere; it's only shown once.
2. Make sure your devices are in Home Assistant and **assigned to areas** (**Settings → Areas, labels & zones**). Iris finds which room a device is in from its area, so an area named "Living Room" becomes the room `living_room`.

### 2. The Iris server

**Install the MQTT broker.** Satellites and the server talk through [Mosquitto](https://mosquitto.org/). On Debian or Ubuntu:

```bash
sudo apt install mosquitto mosquitto-clients python3-venv
```

Mosquitto only accepts local connections by default, so allow the satellites in by creating `/etc/mosquitto/conf.d/iris.conf`:

```
listener 1883
allow_anonymous true
```

```bash
sudo systemctl restart mosquitto
```

Iris doesn't use MQTT passwords or TLS, so keep the broker on your home network. (Or update the code so that it supports proper security ¯\\_(ツ)_/¯)

**Install Iris.** Clone or download this repository onto the server machine, then:

```bash
cd iris/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Configure it.** Edit `server/config.py`:

| Setting | Set it to |
| --- | --- |
| `HA_URL` | Where Home Assistant lives, e.g. `http://192.168.1.50:8123` |
| `HA_TOKEN` | The token from step 1 |
| `MQTT_BROKER` | `127.0.0.1` if Mosquitto runs on this machine (the default), otherwise its address |
| `ROOMS` | Your rooms and the names people say for them, so you can ask for a room by name from another room. Use your Home Assistant area names as the keys (lowercase, underscores for spaces) |
| `MANUAL_ENTITY_ROOMS` | Devices that have no area and no room name at the start of their entity ID. The entries shipped are some examples, so replace or delete them |
| `WEATHER_DEFAULT_CITY`, `_LAT`, `_LON`, `WEATHER_TIMEZONE` | Your city, its coordinates, and timezone |

The scenes in `SCENES` (`server/scene_engine.py`) refer to my best guess on what rooms poeple have are, (`living_room`, `bedroom`, `closet`, `kitchen`, `hallway`), so edit them to match your house.

**Run it.**

```bash
.venv/bin/python main.py
```

You should see `Connected to MQTT broker.`, `Subscribed to 'iris/voice/#'.` and `EntityRegistry: indexed N entities across M room/device buckets.` If N is 0, check `HA_URL`, `HA_TOKEN` and that your devices have areas.

You can test the server before any satellite exists. In one terminal listen for replies, in another send a command as if a satellite had heard it:

```bash
mosquitto_sub -h localhost -t 'iris/speech/#'
mosquitto_pub -h localhost -t iris/voice/bedroom -m '{"room":"bedroom","text":"what time is it"}'
```

**Run it as a service (optional).** Create `/etc/systemd/system/iris-server.service`, adjusting the user and paths:

```ini
[Unit]
Description=Iris server
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
User=iris
WorkingDirectory=/opt/iris/server
ExecStart=/opt/iris/server/.venv/bin/python main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now iris-server
journalctl -fu iris-server
```

### 3. Files for the satellite

The satellite needs a speech-recognition model and, optionally, a text-to-speech voice. 

| What | Needed? | Size | Download |
| --- | --- | --- | --- |
| Speech recognition: Vosk small English model | Required | 40 MB | [`vosk-model-small-en-us-0.15.zip`](https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip) |
| Text to speech: Piper program for 64-bit Raspberry Pi OS | Optional | | [`piper_linux_aarch64.tar.gz`](https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz) |
| Text to speech: Piper voice (US English, Lessac, medium quality) | Needed with Piper | 63 MB | [`en_US-lessac-medium.onnx`](https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx) and [`en_US-lessac-medium.onnx.json`](https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json) |

Without Piper the satellite speaks with espeak-ng, which sounds robotic but works.

**Other choices**

- **Voices:** hear them at the [Piper voice samples](https://rhasspy.github.io/piper-samples/) page and download from the [voice repository](https://huggingface.co/rhasspy/piper-voices/tree/v1.0.0). Each voice is two files, `.onnx` and `.onnx.json`.
- **Piper for 32-bit Raspberry Pi OS:** [`piper_linux_armv7l.tar.gz`](https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_armv7l.tar.gz) instead of the aarch64 file; other platforms are on the [Piper releases](https://github.com/rhasspy/piper/releases) page. The project has been archived (it is read-only), but its release files are still available.
- **Speech models:** the [Vosk model list](https://alphacephei.com/vosk/models). Iris sets its vocabulary at runtime, which only Vosk's small and "lgraph" (dynamic graph) models support. [`vosk-model-en-us-0.22-lgraph.zip`](https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip) (128 MB) should work but hasn't been tested with Iris. Don't use the big static models such as `vosk-model-en-us-0.22` (1.8 GB).

The setup script wants these as `model.zip` and `piper.zip` (downloading zip files is easier than downloading folders), with the Piper voice named `voice.onnx` and `voice.onnx.json`. This builds them in one go:

```bash
mkdir ~/iris-files && cd ~/iris-files

# Speech recognition (required)
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -O model.zip

# Speech output (optional): the Piper program plus a voice
wget https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_aarch64.tar.gz
tar xzf piper_linux_aarch64.tar.gz
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx -O piper/voice.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json -O piper/voice.onnx.json
zip -r piper.zip piper
```

Now get those files to the Pi, either way:

**Option A: serve them over HTTP.** The setup script downloads from the server machine on port 80. From inside `~/iris-files`:

```bash
sudo python3 -m http.server 80
```

Leave it running while the satellite is set up, then stop it. Port 80 needs root; to use another port, change `FILE_SERVER` in `node-setup.sh`.

**Option B: copy them over by hand.** Skip the web server. On the Pi, create `~/iris-satellite/` and put the *unzipped* contents there: the Vosk model folder renamed to `model`, and the `piper` folder (if you want Piper). The setup script skips anything that's already in place.

```bash
unzip model.zip && mv vosk-model-small-en-us-0.15 model
scp -r model piper <user>@<pi-address>:iris-satellite/
```

### 4. The satellite

1. Plug in the USB microphone and speaker, and get the `satellite/` folder onto the Pi (clone this repository, or `scp -r satellite`).
2. Edit `IRIS_SERVER` at the top of `satellite/node-setup.sh` to the address of the Iris server. If your file server or broker is somewhere else, change `FILE_SERVER` and `MQTT_BROKER` too.
3. Run the installer as your normal user (it uses `sudo` where it needs to; don't run it as root), giving the Pi's room a name that matches its Home Assistant area (lowercase, underscores for spaces, such as `living_room`):

   ```bash
   cd satellite
   ./node-setup.sh --room bedroom
   ```

   It installs the packages, creates a Python environment, fetches the model and Piper, configures ALSA, installs the satellite with your room and server address filled in, and registers an `iris-satellite` service. If it doesn't recognise an EMEET device it lists your audio cards (`aplay -l`) and asks which to use.

4. Start it and watch the log:

   ```bash
   sudo systemctl start iris-satellite
   journalctl -fu iris-satellite
   ```

   You should see `Connected to MQTT broker.`, `Microphone open. Iris satellite running.` and `Waiting for wake word 'iris' …`.

`wake.wav` (the chime after the wake word) is included. To use a custom timer alarm, put a `timer.wav` in `satellite/` before running the installer; without one the satellite beeps with espeak-ng. Repeat this section on each Pi with a different `--room`.

If the USB microphone or speaker is unplugged, or drops out, the satellite notices within a few seconds, waits for it, and picks it up again when it comes back. No reboot or restart is needed.

### 5. Try it

Say **"iris"**, wait for the chime, then **"turn on the lights"**. The log shows `Wake word detected.`, `Command recognised: …` and `Speaking: …`, and the lights in that room come on.

### Updating

Pull the new code. On the server, restart the service. On each Pi, re-run `./node-setup.sh --room <room>` and then `sudo systemctl restart iris-satellite`. Re-running is safe and skips downloads you already have; it also makes ALSA select your sound card by name, which is what lets the satellite find a USB device again after it is replugged. Don't just copy `satellite_node.py` over the installed one: the installer writes your room name, broker address and model paths into that copy, and a fresh file would reset them.

## Extending

**Add a voice command.** It needs to be added in up to three places:

1. `ACTION_RULES` in `server/intent_engine.py`: phrases match as substrings and patterns as regexes. The first matching rule wins, so a rule whose phrase is contained in another's must come after it.
2. `server/mqtt_listener.py` (`_build_response`) if it isn't a device action, or `server/ha_controller.py` (`SIMPLE_SERVICES` or the handlers in `__init__`) if it is.
3. The grammar lists in `satellite/satellite_node.py`. **Vosk can only recognise words in `COMMAND_GRAMMAR`**; anything else becomes `[unk]`. Add new words to the relevant list, and add whole phrases to `PHRASES` for commands that are easy to mishear.

**Add a scene.** Append a `Scene` to `SCENES` in `server/scene_engine.py`. Triggers match as substrings.

**Add a room.** Add it to `ROOMS` in `server/config.py` with its spoken names, and the room word to `ROOM_WORDS` in the satellite grammar.

**Add a city for weather by voice.** Add its spoken name to `CITY_NAMES` in `satellite/satellite_node.py`. The speech model has to know every word in it; at startup the satellite logs a `[grammar]` line naming any entries the model lacks, and leaves them out. Open-Meteo resolves a name to its most populous match, so pick names where that's the city you mean.

**Add a device type.** Add it to `DEVICE_KEYWORDS` in `intent_engine.py`, `DOMAIN_MAP` in `entity_registry.py`, and a handler in `ha_controller.py`. Device keywords match as whole words, so list every spoken form ("switch" and "switches").

## Troubleshooting

- **The installer can't download `model.zip`.** Check that the file server is running in the folder that holds the zips, that `IRIS_SERVER` is right, and that the server's firewall allows port 80.
- **The satellite can't connect to MQTT.** From the Pi, run `mosquitto_pub -h <server-address> -t test -m hi`. If that fails, check the Mosquitto config above, that it was restarted, and the server's firewall for port 1883.
- **Commands are heard but nothing happens in Home Assistant.** Look for `No entities found` in the server log. The room name from the satellite has to match a Home Assistant area, and the device has to be assigned to it. Also check `HA_URL` and `HA_TOKEN`.
- **The satellite stops responding after the USB microphone or speaker is unplugged.** It should recover by itself: the log shows `No audio for N s; the microphone was probably disconnected.`, then `No microphone found. Waiting for one to be connected …`, then `Microphone connected.` and `Microphone open.` once it is back. If you see `/etc/asound.conf selects the sound card by number`. If it logs `Reconnecting is stuck. Exiting so the service restarts.`, the audio driver hung and systemd is restarting the service for you.
- **A command is heard as a similar-sounding word.** Add the full phrase to `PHRASES` in `satellite_node.py` so Vosk learns the word order.
- **Speech sounds too fast or too slow.** The satellite reads the voice's sample rate from `voice.onnx.json`, so make sure that file sits next to `voice.onnx`.
- **`Ignoring word missing in vocabulary`.** A grammar entry isn't in the Vosk model's vocabulary. Numbers must be spelled out (`five`, not `5`).
- **Rooms aren't discovered.** Install `websocket-client`; Home Assistant only exposes areas over its WebSocket API.
- **Weather for a city comes back as the default city, or "I didn't catch the city name".** By voice only the cities in `CITY_NAMES` can be recognised, and only after the word "in" ("weather in chicago"). Cities the speech model doesn't know are listed in the `[grammar]` line the satellite logs at startup. Sending `"what is the weather in chicago"` to `iris/voice/<room>` works for any city.

## Note

* Yes this codebase is a mess, but it works :) (At least on my setup)