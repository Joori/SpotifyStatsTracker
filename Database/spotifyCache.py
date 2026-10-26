"""
Shared plumbing for features that cache Spotify data in the user folder.

- CachedJson: a JSON file with a fetchedAt timestamp, kept in memory after the first read.
- Refresher: runs a refresh in a short-lived thread when a page asks for stale data.
  At most one refresh per cache at a time, and after a failure it waits RETRY_AFTER_FAILURE
  before trying again, so nothing hammers Spotify. Nothing polls on a timer.
"""
import json
import threading
import time
from pathlib import Path

try:
    from Database.spotifyApi import RateLimitedError, SpotifyAuthError
except ModuleNotFoundError:
    from spotifyApi import RateLimitedError, SpotifyAuthError

RETRY_AFTER_FAILURE = 15 * 60
BACKGROUND_MAX_WAIT = 30    #< background refreshes may wait out short 429s; web requests never wait


class CachedJson:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = None

    def load(self):
        with self._lock:
            if self._data is None:
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._data = {}
            return self._data

    def save(self, data):
        data = dict(data, fetchedAt=time.time())
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self._data = data
        return data

    def age(self):
        fetchedAt = self.load().get("fetchedAt")
        return time.time() - fetchedAt if fetchedAt else None

    def isStale(self, maxAge):
        age = self.age()
        return age is None or age > maxAge


class Refresher:
    def __init__(self, name, refreshFn, api):
        self.name = name
        self.refreshFn = refreshFn
        self.api = api
        self._lock = threading.Lock()
        self._running = False
        self._lastFailure = 0.0
        self.lastError = None
        self.thread = None

    @property
    def running(self):
        return self._running

    def _canTry(self):
        if self._running or time.time() - self._lastFailure < RETRY_AFTER_FAILURE:
            return False
        return self.api is not None and self.api.isConnected()

    def refreshNow(self):
        """ Run in the calling thread. Returns True on success. """
        with self._lock:
            if self._running:
                return False
            self._running = True
        try:
            self.refreshFn()
            self.lastError = None
            return True
        except (RateLimitedError, SpotifyAuthError) as e:
            self._fail(str(e))
        except Exception as e:   #< a broken refresh must never take a page down
            self._fail(f"{type(e).__name__}: {e}")
        finally:
            self._running = False
        return False

    def _fail(self, message):
        self._lastFailure = time.time()
        self.lastError = message
        print(f"{self.name} refresh failed (retrying in {RETRY_AFTER_FAILURE // 60} min): {message}")

    def refreshInBackground(self):
        """ Start a refresh thread if none is running and we're not backing off. Never blocks. """
        if not self._canTry():
            return False
        self.thread = threading.Thread(target=self.refreshNow, daemon=True, name=f"refresh-{self.name}")
        self.thread.start()
        return True
