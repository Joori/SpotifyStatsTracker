"""
Shared client for the official Spotify Web API (spotipy + OAuth auth-code flow).

- Tokens are cached in secrets/ (mounted volume) and refreshed automatically.
- A refresh that fails with invalid_grant (refresh tokens expire after ~6 months)
  puts the app into a "needs relog" state instead of raising out of a thread.
- A 429 starts a shared cooldown. Callers choose how long they are willing to
  wait (maxWait). Web/login code passes 0 so it never blocks on Spotify.
"""
import os
import time
import threading
from pathlib import Path

import requests
import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "user-read-recently-played playlist-read-private"
DEFAULT_MAX_RETRY_WAIT = 300      #< seconds; longer Retry-After values fail fast instead of waiting
DEFAULT_RETRY_AFTER = 5           #< used when Spotify sends a 429 without a Retry-After header
MAX_CONSECUTIVE_429 = 5


class SpotifyAuthError(Exception):
    """ No usable token. reason: notConfigured | notConnected | expired """
    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or reason)


class RateLimitedError(Exception):
    """ Spotify asked us to back off for longer than the caller is willing to wait. """
    def __init__(self, retryAt):
        self.retryAt = retryAt
        super().__init__(f"Spotify rate limit, retry after {time.strftime('%H:%M:%S', time.localtime(retryAt))}")


def _envInt(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class _TokenProvider:
    """ Minimal auth_manager for spotipy.Spotify; never falls back to an interactive prompt. """
    def __init__(self, api):
        self.api = api

    def get_access_token(self, as_dict=False):
        return self.api.accessToken()


class SpotifyApi:
    def __init__(self, cacheFile, clientId=None, clientSecret=None, redirectUri=None, maxRetryWait=None):
        self.clientId = clientId or os.environ.get("SPOTIFY_CLIENT_ID", "")
        self.clientSecret = clientSecret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        self.redirectUri = redirectUri or os.environ.get("SPOTIFY_REDIRECT_URI", "")
        self.maxRetryWait = maxRetryWait if maxRetryWait is not None else _envInt("SPOTIFY_MAX_RETRY_WAIT", DEFAULT_MAX_RETRY_WAIT)

        self.cacheFile = Path(cacheFile)
        self.cacheFile.parent.mkdir(parents=True, exist_ok=True)
        self.cacheHandler = CacheFileHandler(cache_path=str(self.cacheFile))

        self._tokenLock = threading.RLock()
        self._callLock = threading.Lock()      #< one request at a time across listener + importer
        self._stateLock = threading.Lock()
        self.cooldownUntil = 0.0
        self.authProblem = None

        self.oauth = None
        if self.isConfigured():
            self.oauth = SpotifyOAuth(
                client_id=self.clientId,
                client_secret=self.clientSecret,
                redirect_uri=self.redirectUri,
                scope=SCOPES,
                cache_handler=self.cacheHandler,
                open_browser=False,
                requests_timeout=10,
            )
        else:
            print("Spotify API not configured: set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI")

        self.client = spotipy.Spotify(
            auth_manager=_TokenProvider(self),
            requests_timeout=10,
            retries=2,
            status_retries=2,
            status_forcelist=(500, 502, 503, 504),   #< 429 handled below so Retry-After is honoured and capped
        )

    # ---------- auth ----------
    def isConfigured(self):
        return bool(self.clientId and self.clientSecret and self.redirectUri)

    def isConnected(self):
        """ Cheap check, no network: is there a token we expect to work? """
        if not self.isConfigured() or self.authProblem == "expired":
            return False
        return self.cacheHandler.get_cached_token() is not None

    def needsRelog(self):
        return self.authProblem == "expired"

    def getAuthorizeUrl(self, state):
        if not self.oauth:
            raise SpotifyAuthError("notConfigured")
        return self.oauth.get_authorize_url(state=state)

    def handleCallback(self, code):
        """ Exchange the auth code for tokens (saved to the cache file). """
        if not self.oauth:
            raise SpotifyAuthError("notConfigured")
        with self._tokenLock:
            self.cacheFile.parent.mkdir(parents=True, exist_ok=True)
            self.oauth.get_access_token(code=code, check_cache=False, as_dict=True)
            self.authProblem = None

    def accessToken(self):
        with self._tokenLock:
            if not self.oauth:
                raise SpotifyAuthError("notConfigured")
            info = self.cacheHandler.get_cached_token()
            if not info or not info.get("access_token"):
                self.authProblem = "notConnected"
                raise SpotifyAuthError("notConnected")
            if not self.oauth._is_scope_subset(SCOPES, info.get("scope", "")):
                self.authProblem = "expired"   #< scopes changed; user has to grant them again
                raise SpotifyAuthError("expired", "Token is missing required scopes")
            if self.oauth.is_token_expired(info):
                try:
                    info = self.oauth.refresh_access_token(info["refresh_token"])
                except SpotifyOauthError as e:
                    if getattr(e, "error", None) in ("invalid_grant", "invalid_client", "unauthorized_client"):
                        print(f"Spotify refresh token rejected ({e.error}); relog required")
                        self.authProblem = "expired"
                        raise SpotifyAuthError("expired", str(e)) from e
                    raise
            self.authProblem = None
            return info["access_token"]

    def checkToken(self):
        """ Refresh the token if needed. Never raises; updates authProblem. """
        try:
            self.accessToken()
        except SpotifyAuthError:
            pass
        except (requests.RequestException, SpotifyOauthError) as e:
            print(f"Spotify token check failed (will retry): {e}")

    # ---------- rate limiting ----------
    def cooldownRemaining(self):
        with self._stateLock:
            return max(0.0, self.cooldownUntil - time.time())

    def _startCooldown(self, seconds):
        with self._stateLock:
            self.cooldownUntil = max(self.cooldownUntil, time.time() + seconds)
            until = self.cooldownUntil
        print(f"Spotify rate limit hit, backing off {int(seconds)}s (until {time.strftime('%H:%M:%S', time.localtime(until))})")

    @staticmethod
    def _retryAfterSeconds(error):
        headers = getattr(error, "headers", None) or {}
        value = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return max(1, int(float(value)))
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER

    def status(self):
        remaining = self.cooldownRemaining()
        return {
            "configured": self.isConfigured(),
            "connected": self.isConnected(),
            "needsRelog": self.needsRelog(),
            "rateLimitedFor": int(remaining) if remaining > 0 else 0,
        }

    def call(self, fn, maxWait=0, onWait=None):
        """
        Run fn(spotipyClient). On 429, wait up to maxWait seconds (calling onWait(secondsLeft)
        about every 5s so the UI can show it). If Spotify wants a longer pause, raise RateLimitedError.
        """
        consecutive429 = 0
        while True:
            remaining = self.cooldownRemaining()
            if remaining > 0:
                if remaining > maxWait:
                    raise RateLimitedError(time.time() + remaining)
                self._waitOut(remaining, onWait)
                continue

            with self._callLock:
                try:
                    return fn(self.client)
                except SpotifyException as e:
                    if e.http_status == 429:
                        consecutive429 += 1
                        self._startCooldown(self._retryAfterSeconds(e))
                        if consecutive429 >= MAX_CONSECUTIVE_429:
                            raise RateLimitedError(time.time() + self.cooldownRemaining()) from e
                        continue
                    if e.http_status == 401:
                        self.checkToken()
                    raise

    @staticmethod
    def _waitOut(seconds, onWait):
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            if onWait:
                try:
                    onWait(int(left) + 1)
                except Exception:
                    pass
            time.sleep(min(5, left))

    # ---------- convenience wrappers (never wait: safe from web/listener threads) ----------
    def track(self, trackId, maxWait=0):
        return self.call(lambda c: c.track(trackId), maxWait=maxWait)

    def artist(self, artistId, maxWait=0):
        return self.call(lambda c: c.artist(artistId), maxWait=maxWait)

    def albumName(self, albumId):
        return (self.call(lambda c: c.album(albumId)) or {}).get("name", "Unknown Album")

    def playlistName(self, playlistId):
        return (self.call(lambda c: c.playlist(playlistId, fields="name")) or {}).get("name", "Unknown Playlist")

    def recentlyPlayed(self, limit=50):
        return (self.call(lambda c: c.current_user_recently_played(limit=limit)) or {}).get("items") or []
