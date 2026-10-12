import logging
from dataclasses import dataclass, field

from entity_registry import EntityRegistry
from ha_controller import HomeAssistantController
from weather_engine import WeatherEngine

logger = logging.getLogger(__name__)


@dataclass
class SceneStep:
    device: str
    action: str
    room: str | None = None      # None applies the step to every room that has this device
    target: str | None = None    # friendly name of one specific entity; None means all in the room
    parameters: dict = field(default_factory=dict)


@dataclass
class Scene:
    name: str
    triggers: list[str]
    steps: list[SceneStep]
    response: str


SCENES: list[Scene] = [
    Scene(
        name="sleep",
        triggers=["time for sleep", "night night", "goodnight", "good night",
                  "sleep time", "going to bed", "bedtime"],
        steps=[
            SceneStep("light", "turn_off", room="living_room"),
            SceneStep("light", "turn_off", room="bedroom"),
            SceneStep("light", "turn_off", room="closet"),
        ],
        response="Goodnight. Turning off all the lights.",
    ),
    Scene(
        name="cozy",
        triggers=["set the lights to cozy", "cozy mode", "cozy lights", "make it cozy"],
        steps=[
            SceneStep("light", "turn_on", room="living_room",
                      parameters={"brightness": 30, "color_temp_kelvin": 2700}),
            SceneStep("light", "turn_on", room="bedroom",
                      parameters={"brightness": 10, "color_temp_kelvin": 2700}),
            SceneStep("light", "turn_off", room="kitchen"),
            SceneStep("light", "turn_off", room="hallway"),
        ],
        response="Setting the mood. Cozy lights on.",
    ),
    Scene(
        name="morning",
        triggers=["good morning", "morning mode", "wake up lights", "rise and shine", "wake up"],
        steps=[
            SceneStep("light", "turn_on", room="bedroom",
                      parameters={"brightness": 40, "color_temp_kelvin": 2700}),
            SceneStep("light", "turn_on", room="living_room",
                      parameters={"brightness": 80, "color_temp_kelvin": 2700}),
        ],
        response="Good morning! Lights on.",
    ),
    Scene(
        name="movie",
        triggers=["movie mode", "movie time", "watching a movie", "film time"],
        steps=[
            SceneStep("light", "set_brightness", room="living_room", parameters={"brightness": 15}),
            SceneStep("light", "turn_off", room="bedroom"),
            SceneStep("light", "turn_off", room="closet"),
        ],
        response="Enjoy the movie. Lights dimmed.",
    ),
]


class SceneEngine:
    def __init__(
        self,
        ha_controller: HomeAssistantController,
        registry: EntityRegistry,
        weather_engine: WeatherEngine,
    ):
        self.ha = ha_controller
        self.registry = registry

        self._trigger_map = {
            trigger.lower().strip(): scene
            for scene in SCENES
            for trigger in scene.triggers
        }
        # Longest first so a specific trigger wins over a shorter one it contains.
        self._triggers_longest_first = sorted(self._trigger_map, key=len, reverse=True)

        # Scenes whose spoken response needs live data instead of a static string.
        self._dynamic_responses = {"morning": weather_engine.morning_brief}

        logger.info(f"SceneEngine: loaded {len(SCENES)} scenes ({len(self._trigger_map)} triggers).")

    def match(self, text: str) -> Scene | None:
        text = text.lower().strip()
        for trigger in self._triggers_longest_first:
            if trigger in text:
                scene = self._trigger_map[trigger]
                logger.info(f"Scene matched: '{scene.name}' (trigger='{trigger}', text='{text}')")
                return scene
        return None

    def get_response(self, scene: Scene) -> str:
        response_fn = self._dynamic_responses.get(scene.name)
        if response_fn is not None:
            try:
                return response_fn()
            except Exception as e:
                logger.error(
                    f"Scene '{scene.name}': dynamic response failed: {e}. Using static response.",
                    exc_info=True,
                )
        return scene.response

    def execute(self, scene: Scene) -> bool:
        logger.info(f"Executing scene '{scene.name}' ({len(scene.steps)} steps)…")
        results = []

        for step in scene.steps:
            rooms = self._rooms_for(step)
            if not rooms:
                logger.warning(
                    f"Scene '{scene.name}': step '{step.action} {step.device}' has no room and "
                    "no rooms with this device were found in the registry. Skipping."
                )
                continue

            for room in rooms:
                intent = {
                    "device":      step.device,
                    "action":      step.action,
                    "room":        room,
                    "target_name": step.target,
                    "parameters":  step.parameters,
                }
                try:
                    ok = self.ha.execute(intent)
                except Exception as e:
                    logger.error(f"Scene '{scene.name}': step exception — {e}", exc_info=True)
                    ok = False
                results.append(ok)
                if not ok:
                    logger.warning(
                        f"Scene '{scene.name}': step failed — {step.action} {step.device} in {room}"
                    )

        success = bool(results) and all(results)
        logger.info(f"Scene '{scene.name}' {'completed' if success else 'completed with errors'}.")
        return success

    def _rooms_for(self, step: SceneStep) -> list[str]:
        if step.room:
            return [step.room]
        if step.target:
            # A named target is matched by name across the whole house; the room is
            # only a fallback if that lookup fails.
            return ["living_room"]
        return self.registry.rooms_with_device(step.device)
