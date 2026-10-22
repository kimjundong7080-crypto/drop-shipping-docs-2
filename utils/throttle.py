import time
import threading


class TokenBucket:
    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = rate
        self.capacity = capacity or max(1.0, rate)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, amount: float = 1.0) -> None:
        with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.last) * self.rate,
                )
                self.last = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                time.sleep((amount - self.tokens) / self.rate)
