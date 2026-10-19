import asyncio
import time


class TokenBucketRateLimiter:
    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be greater than 0")
        self.rate_per_second = float(rate_per_second)
        self.capacity = float(capacity) if capacity is not None else float(rate_per_second)
        if self.capacity <= 0:
            raise ValueError("capacity must be greater than 0")
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
        self._last_refill = now

    def _time_until_token(self) -> float:
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self.rate_per_second

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = self._time_until_token()
            await asyncio.sleep(wait_seconds)

    async def __aenter__(self) -> "TokenBucketRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None


class SyncTokenBucketRateLimiter:
    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be greater than 0")
        self.rate_per_second = float(rate_per_second)
        self.capacity = float(capacity) if capacity is not None else float(rate_per_second)
        if self.capacity <= 0:
            raise ValueError("capacity must be greater than 0")
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
        self._last_refill = now

    def acquire(self) -> None:
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            wait_seconds = (1.0 - self._tokens) / self.rate_per_second
            time.sleep(wait_seconds)
