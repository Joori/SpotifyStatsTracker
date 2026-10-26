import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spotipy.exceptions import SpotifyException

from Database.nowPlaying import NowPlaying, parse
from Database.spotifyApi import RateLimitedError

TRACK = {
    "currently_playing_type": "track", "is_playing": True, "progress_ms": 60_000,
    "item": {
        "id": "t1", "name": "Pratigi", "duration_ms": 200_000, "is_local": False,
        "external_urls": {"spotify": "https://open.spotify.com/track/t1"},
        "artists": [{"name": "Vegas (Brazil)"}, {"name": "Guest"}],
        "album": {"name": "Album", "images": [
            {"url": "https://i.scdn.co/640", "width": 640}, {"url": "https://i.scdn.co/300", "width": 300},
            {"url": "https://i.scdn.co/64", "width": 64}]},
    },
}
EPISODE = {
    "currently_playing_type": "episode", "is_playing": False, "progress_ms": 5_000,
    "item": {"name": "Ep 1", "duration_ms": 3_600_000, "images": [{"url": "https://i.scdn.co/ep", "width": 300}],
             "show": {"name": "Some Show"}, "external_urls": {"spotify": "https://open.spotify.com/episode/e1"}},
}
AD = {"currently_playing_type": "ad", "is_playing": True, "progress_ms": 1000, "item": None}
UNKNOWN = {"currently_playing_type": "unknown", "is_playing": True, "progress_ms": 1000, "item": None}


class FakeApi:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.connected = True

    def isConnected(self):
        return self.connected

    def call(self, fn, maxWait=0, onWait=None):
        self.calls.append(maxWait)
        if self.error:
            raise self.error
        client = MagicMock()
        client._get.side_effect = lambda url, **kw: self.response
        return fn(client)


class ParseTests(unittest.TestCase):
    def testTrack(self):
        r = parse(TRACK)
        self.assertEqual((r["type"], r["title"], r["subtitle"]), ("track", "Pratigi", "Vegas (Brazil), Guest"))
        self.assertEqual((r["progressMs"], r["durationMs"], r["isPlaying"]), (60_000, 200_000, True))
        self.assertEqual(r["imageUrl"], "https://i.scdn.co/300")
        self.assertEqual(r["url"], "https://open.spotify.com/track/t1")

    def testNothingPlaying(self):   #< Spotify answers 204, spotipy returns None
        r = parse(None)
        self.assertTrue(r["available"])
        self.assertFalse(r["active"])

    def testPausedPodcastEpisode(self):
        r = parse(EPISODE)
        self.assertEqual((r["type"], r["title"], r["subtitle"], r["isPlaying"]), ("episode", "Ep 1", "Some Show", False))
        self.assertEqual(r["imageUrl"], "https://i.scdn.co/ep")

    def testAdAndUnknownHaveNoTrackDetails(self):
        ad = parse(AD)
        self.assertEqual((ad["type"], ad["title"], ad["progressMs"], ad["imageUrl"]), ("ad", "Advertisement", None, None))
        unknown = parse(UNKNOWN)
        self.assertEqual((unknown["type"], unknown["title"]), ("unknown", None))


class NowPlayingTests(unittest.TestCase):
    def testResultIsReusedForTtlThenRefetched(self):
        clock = [1000.0]
        api = FakeApi(TRACK)
        store = NowPlaying(api, ttl=10, clock=lambda: clock[0])
        store.get()
        clock[0] += 9
        store.get()
        self.assertEqual(len(api.calls), 1)
        clock[0] += 2
        store.get()
        self.assertEqual(len(api.calls), 2)

    def testNeverWaitsOnRateLimit(self):
        api = FakeApi(error=RateLimitedError(time.time() + 600))
        result = NowPlaying(api).get()
        self.assertEqual(result, {"available": False, "reason": "rateLimited", "fetchedAt": result["fetchedAt"]})
        self.assertEqual(api.calls, [0])

    def testSpotifyErrorDoesNotRaise(self):
        api = FakeApi(error=SpotifyException(500, -1, "boom"))
        self.assertEqual(NowPlaying(api).get()["reason"], "error")

    def testNoCallWhenNotConnected(self):
        api = FakeApi(TRACK)
        api.connected = False
        self.assertEqual(NowPlaying(api).get(), {"available": False, "reason": "notConnected"})
        self.assertEqual(api.calls, [])

    def testSummaryBlock(self):
        self.assertEqual(NowPlaying(FakeApi(TRACK)).summary(), {
            "is_playing": True, "type": "track", "track": "Pratigi", "artist": "Vegas (Brazil), Guest",
            "album": "Album", "progress_ms": 60_000, "duration_ms": 200_000})
        self.assertIsNone(NowPlaying(FakeApi(None)).summary())
        self.assertIsNone(NowPlaying(FakeApi(error=RateLimitedError(time.time() + 60))).summary())


class NowPlayingRouteTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from app import SpotifyDashboardApp
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        dashboard = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dashboard.app = Flask("app", template_folder=str(root / "templates"), static_folder=str(root / "static"))
        dashboard.spotify = MagicMock(redirectUri="https://host/callback")
        dashboard.pendingOAuthStates = {}
        dashboard.database = MagicMock(listener=None)
        dashboard.nowPlaying = NowPlaying(FakeApi(TRACK))
        dashboard.registerRoutes()
        self.client = dashboard.app.test_client()

    def testEndpointReturnsStateAndIsNotCached(self):
        response = self.client.get("/api/now-playing")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.get_json()["title"], "Pratigi")


if __name__ == "__main__":
    unittest.main()
