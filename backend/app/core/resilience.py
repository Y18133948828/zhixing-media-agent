import asyncio
import time


class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    """Per-process breaker; one probe in half-open state."""

    def __init__(self, threshold=5, cooldown=30):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures, self.open_until, self.probing = 0, 0.0, False
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            if self.open_until:
                if time.monotonic() < self.open_until or self.probing:
                    raise CircuitOpen("模型服务熔断冷却中，请稍后重试")
                self.probing = True

    async def success(self):
        async with self.lock:
            self.failures, self.open_until, self.probing = 0, 0.0, False

    async def failure(self):
        async with self.lock:
            self.failures += 1
            if self.probing or self.failures >= self.threshold:
                self.open_until = time.monotonic() + self.cooldown
            self.probing = False

    async def cancelled(self):
        async with self.lock:
            self.probing = False
