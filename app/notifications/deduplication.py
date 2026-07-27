import hashlib
import time


class FingerprintDeduplicator:
    def __init__(self, cooldown_seconds: int):
        self.cooldown_seconds = cooldown_seconds
        self._seen: dict[str, float] = {}

    def accept(self, payload: str) -> bool:
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        now = time.monotonic()
        previous = self._seen.get(fingerprint)
        if previous is not None and now - previous < self.cooldown_seconds:
            return False
        self._seen[fingerprint] = now
        return True

