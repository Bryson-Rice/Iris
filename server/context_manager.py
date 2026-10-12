import logging
import time

from config import CONTEXT_TTL_SECONDS

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self):
        self._context: dict[str, dict] = {}

    def update(self, room: str, intent: dict) -> None:
        self._context[room] = {"intent": intent, "timestamp": time.time()}
        logger.debug(f"Context updated for '{room}': {intent}")

    def get(self, room: str) -> dict | None:
        entry = self._context.get(room)
        if entry is None:
            return None

        age = time.time() - entry["timestamp"]
        if age > CONTEXT_TTL_SECONDS:
            logger.debug(f"Context for '{room}' expired ({age:.1f}s old); discarding.")
            del self._context[room]
            return None

        return entry["intent"]
