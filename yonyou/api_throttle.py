"""Process-wide outbound pacing. These defaults are local policy, not vendor quotas."""
import math
import os
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(value, now=None):
    """Parse HTTP Retry-After seconds or date, without retaining response headers."""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(str(value))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = stamp.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0, math.ceil(seconds))


class ThrottleBusy(Exception):
    def __init__(self, seconds, cooling=False):
        self.retry_after_seconds = max(1, math.ceil(seconds))
        self.cooling = cooling
        super().__init__('用友接口正在冷却' if cooling else '用友请求队列繁忙')


class ApiThrottle:
    def __init__(self, min_interval=0.5, cooldown=30, max_wait=5,
                 clock=time.monotonic, sleep=time.sleep):
        if any(not math.isfinite(float(v)) or v < 0 for v in (min_interval, cooldown, max_wait)):
            raise ValueError('节流配置必须为有限非负数')
        self.min_interval = min_interval
        self.cooldown = cooldown
        self.max_wait = max_wait
        self.clock, self.sleep = clock, sleep
        self._next = 0
        self._cooling_until = 0
        self._lock = threading.Lock()

    def acquire(self):
        deadline = self.clock() + self.max_wait
        while True:
            remaining = max(0, deadline - self.clock())
            if not self._lock.acquire(timeout=remaining):
                raise ThrottleBusy(self.min_interval)
            try:
                now = self.clock()
                if now < self._cooling_until:
                    raise ThrottleBusy(self._cooling_until - now, cooling=True)
                delay = max(0, self._next - now)
                if delay > max(0, deadline - now):
                    raise ThrottleBusy(delay)
                if not delay:
                    # Only a ready sender takes a slot; sleepers reserve nothing.
                    self._next = now + self.min_interval
                    return
            finally:
                self._lock.release()
            # A 429 must be able to start cooldown while another sender waits.
            # Waking senders recheck cooldown and compete atomically for one slot.
            self.sleep(delay)

    def block(self, seconds=None):
        seconds = self.cooldown if seconds is None else max(1, float(seconds))
        with self._lock:
            self._cooling_until = max(self._cooling_until, self.clock() + seconds)
            return max(1, math.ceil(self._cooling_until - self.clock()))


_SHARED = None
_SHARED_LOCK = threading.Lock()


def shared_throttle():
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            value = os.environ.get('YONYOU_API_MIN_INTERVAL_SECONDS', '0.5')
            try:
                interval = float(value)
                if not math.isfinite(interval) or not 0.1 <= interval <= 5:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('YONYOU_API_MIN_INTERVAL_SECONDS 应为0.1至5秒') from None
            _SHARED = ApiThrottle(min_interval=interval)
        return _SHARED
