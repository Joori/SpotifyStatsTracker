"""
What's playing right now (GET /me/player/currently-playing). Only called when a page or the
dashboard widget asks: never polled in the background. A result is reused for TTL seconds so
several open tabs (or Homepage) don't multiply calls, and web requests never wait on a 429.
"""
import threading
import time

from spotipy.exceptions import SpotifyException

try:
    from Database.spotifyApi import RateLimitedError, SpotifyAuthError
except ModuleNotFoundError:
    from spotifyApi import RateLimitedError, SpotifyAuthError

TTL = 10
COVER_TARGET_WIDTH = 300


def pickImageUrl(images, target=COVER_TARGET_WIDTH):
    """ Smallest image at least `target` wide, else the largest. """
    images = [i for i in images or [] if isinstance(i, dict) and i.get("url")]
    if not images:
        return None
    images.sort(key=lambda i: i.get("width") or 0)
    for image in images:
        if (image.get("width") or 0) >= target:
            return image["url"]
    return images[-1]["url"]


def parse(data):
    """ Spotify's currently-playing response (None = 204) -> the shape /api/now-playing returns. """
    if not data:
        return {"available": True, "active": False, "isPlaying": False, "type": None}
    kind = data.get("currently_playing_type") or "unknown"
    item = data.get("item") or None
    result = {
        "available": True,
        "active": True,
        "isPlaying": bool(data.get("is_playing")),
        "type": kind,
        "progressMs": data.get("progress_ms"),
        "durationMs": None,
        "title": None,
        "subtitle": None,
        "album": None,
        "imageUrl": None,
        "url": None,
        "trackId": None,
        "isLocal": False,
    }
    if kind == "track" and item:
        album = item.get("album") or {}
        result.update({
            "title": item.get("name"),
            "subtitle": ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name")),
            "album": album.get("name"),
            "imageUrl": pickImageUrl(album.get("images")),
            "durationMs": item.get("duration_ms"),
            "url": (item.get("external_urls") or {}).get("spotify"),
            "trackId": item.get("id"),
            "isLocal": bool(item.get("is_local")),
        })
    elif kind == "episode" and item:
        show = item.get("show") or {}
        result.update({
            "title": item.get("name"),
            "subtitle": show.get("name") or "Podcast",
            "imageUrl": pickImageUrl(item.get("images") or show.get("images")),
            "durationMs": item.get("duration_ms"),
            "url": (item.get("external_urls") or {}).get("spotify"),
        })
    elif kind == "ad":
        result.update({"title": "Advertisement", "progressMs": None})
    else:
        result.update({"type": "unknown", "title": None, "progressMs": None})
    return result


class NowPlaying:
    def __init__(self, api, ttl=TTL, clock=time.time):
        self.api = api
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._cached = None
        self._cachedAt = 0.0

    def get(self):
        if self.api is None or not self.api.isConnected():
            return {"available": False, "reason": "notConnected"}
        with self._lock:   #< concurrent requests share one Spotify call
            now = self.clock()
            if self._cached is not None and now - self._cachedAt < self.ttl:
                return self._cached
            try:
                data = self.api.call(
                    lambda c: c._get("me/player/currently-playing", additional_types="track,episode"), maxWait=0)
                result = parse(data)
            except RateLimitedError:
                result = {"available": False, "reason": "rateLimited"}
            except SpotifyAuthError:
                result = {"available": False, "reason": "notConnected"}
            except SpotifyException as e:
                print(f"Now playing lookup failed ({e.http_status})")
                result = {"available": False, "reason": "error"}
            except Exception as e:
                print(f"Now playing lookup failed: {e}")
                result = {"available": False, "reason": "error"}
            result["fetchedAt"] = int(now * 1000)
            self._cached, self._cachedAt = result, now
            return result

    def summary(self):
        """ Compact snake_case block for /api/summary; None when nothing is playing or it's unavailable. """
        state = self.get()
        if not state.get("available") or not state.get("active"):
            return None
        return {
            "is_playing": state.get("isPlaying", False),
            "type": state.get("type"),
            "track": state.get("title"),
            "artist": state.get("subtitle"),
            "album": state.get("album"),
            "progress_ms": state.get("progressMs"),
            "duration_ms": state.get("durationMs"),
        }
