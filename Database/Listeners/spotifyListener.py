import os
import threading

try:
    from Database.spotifyApi import RateLimitedError, SpotifyAuthError
    from Database.utils import parseError
except ModuleNotFoundError:
    from spotifyApi import RateLimitedError, SpotifyAuthError
    from utils import parseError

DEFAULT_POLL_INTERVAL = 120


def _pollIntervalFromEnv():
    try:
        return max(30, int(os.environ.get("SPOTIFY_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)))
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL


class Listener:
    """ Polls the official recently-played endpoint. De-duplication happens in the callback. """

    def __init__(self, api, pollInterval=None):
        self.api = api
        self.pollInterval = pollInterval or _pollIntervalFromEnv()
        self._stopEvent = threading.Event()
        self.thread = None

    def isLoggedIn(self):
        return self.api.isConnected()

    def pollOnce(self, callback):
        if not self.api.isConnected():
            return
        try:
            items = self.api.recentlyPlayed(limit=50)
        except RateLimitedError as e:
            print(f"Listener paused: {e}")
            return
        except SpotifyAuthError as e:
            print(f"Listener paused, Spotify login needed ({e.reason})")
            return
        if items:
            callback(items)

    def startListener(self, callback):
        print(f"Recently-played listener polling every {self.pollInterval}s")
        while not self._stopEvent.is_set():
            try:
                self.pollOnce(callback)
            except Exception as e:
                print("Error in listener:", parseError(e))
            self._stopEvent.wait(self.pollInterval)

    def startListener_thread(self, callback):
        self.thread = threading.Thread(target=self.startListener, args=(callback,), daemon=True)
        self.thread.start()

    def stop(self):
        self._stopEvent.set()
