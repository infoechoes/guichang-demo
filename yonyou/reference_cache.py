"""Bounded process-local cache of successfully verified product archives.

Owned by a connector client, so restarting/reconnecting never trusts browser IDs.
Neither exceptions nor order data are retained. Concurrent identical reads share
one flight; callers always receive their own copy of the archive.
"""
import copy
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future


class ReferenceCache:
    def __init__(self, ttl=900, max_entries=2048, clock=time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self._values = OrderedDict()
        self._flights = {}
        self._lock = threading.Lock()

    def invalidate(self, keys):
        """Invalidate a refresh batch up front, including its unscheduled rows."""
        with self._lock:
            for key in keys:
                self._values.pop(key, None)

    def peek(self, key):
        """Read an unexpired success without starting or waiting for a request."""
        with self._lock:
            stored = self._values.get(key)
            if stored and stored[0] > self.clock():
                self._values.move_to_end(key)
                return copy.deepcopy(stored[1])
            self._values.pop(key, None)
            return None

    def get(self, key, loader, force=False):
        """Return (archive, reused); force bypasses values but joins in-flight reads."""
        with self._lock:
            future = self._flights.get(key)
            if force and future is None:
                self._values.pop(key, None)
            stored = self._values.get(key)
            if stored and stored[0] > self.clock():
                self._values.move_to_end(key)
                return copy.deepcopy(stored[1]), True
            self._values.pop(key, None)
            owner = future is None
            if owner:
                future = self._flights[key] = Future()
        if not owner:
            return copy.deepcopy(future.result()), True
        try:
            value = loader()
            with self._lock:
                self._values[key] = (self.clock() + self.ttl, copy.deepcopy(value))
                self._values.move_to_end(key)
                while len(self._values) > self.max_entries:
                    self._values.popitem(last=False)
            future.set_result(value)
            return copy.deepcopy(value), False
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)


_creation_lock = threading.Lock()


def client_cache(client):
    with _creation_lock:
        cache = getattr(client, '_verified_product_cache', None)
        if cache is None:
            cache = ReferenceCache()
            client._verified_product_cache = cache
        return cache
