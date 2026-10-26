import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from spotipy.exceptions import SpotifyException, SpotifyOauthError

from Database.database import Database
from Database import analytics, playMatching
from Database.spotifyApi import SpotifyApi, RateLimitedError, SpotifyAuthError
from Database.utils import timeToInt

DURATION_MS = 200_000          #< 3m20s
DURATION_S = DURATION_MS // 1000
BASE = 1_760_000_000           #< arbitrary fixed point in time (unix seconds)


def rawTrack(trackId, name, artist="Artist One"):
    return {
        "id": trackId,
        "type": "track",
        "name": name,
        "external_urls": {"spotify": f"https://open.spotify.com/track/{trackId}"},
        "duration_ms": DURATION_MS,
        "explicit": False,
        "disc_number": 1,
        "track_number": 1,
        "external_ids": {"isrc": "ISRC" + trackId},
        "album": {
            "id": "album-" + trackId,
            "name": "Album",
            "external_urls": {"spotify": "https://open.spotify.com/album/x"},
            "images": [{"url": "https://example.com/a.jpg"}],
            "total_tracks": 10,
            "release_date": "2020",
            "release_date_precision": "year",
            "artists": [{"id": "artist-1", "name": artist, "external_urls": {"spotify": "https://open.spotify.com/artist/artist-1"}}],
        },
    }


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def recentlyPlayedItem(track, endTs):
    return {"track": track, "played_at": iso(endTs), "context": None}


def extendedRow(track, endTs, msPlayed, skipped=None):
    return {
        "ts": iso(endTs),
        "ms_played": msPlayed,
        "master_metadata_track_name": track["name"],
        "master_metadata_album_artist_name": track["album"]["artists"][0]["name"],
        "spotify_track_uri": f"spotify:track:{track['id']}",
        "skipped": skipped,
        "reason_end": "fwdbtn" if skipped else "trackdone",
    }


class FakeApi:
    maxRetryWait = 0

    def __init__(self, tracksById):
        self.tracksById = tracksById
        self.client = MagicMock()
        self.client.track.side_effect = lambda tid: self.tracksById[tid]
        self.client.artist.return_value = {"images": []}

    def call(self, fn, maxWait=0, onWait=None):
        return fn(self.client)

    def track(self, tid, maxWait=0):
        return self.tracksById[tid]

    def artist(self, aid, maxWait=0):
        return {"images": []}

    def isConnected(self):
        return True


class TempDatabase(Database):
    """ Real Database, but every file lives in a temp folder and images aren't downloaded. """
    def __init__(self, folder, api):
        super().__init__(user="test", spotifyApi=api)
        folder = Path(folder)
        self.entriesPath = folder / "entries.json"
        self.tracksPath = folder / "tracks.json"
        self.playlistsPath = folder / "playlists.json"
        self.progressPath = folder / "progress.json"
        self.imgDir_tracks = folder / "img" / "tracks"
        self.imgDir_artists = folder / "img" / "artists"

    def saveImagesFromTrack(self, track):
        pass


class ListenerThenImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.songA = rawTrack("trackA", "Song A")
        self.songB = rawTrack("trackB", "Song B")
        self.songC = rawTrack("trackC", "Song C")
        self.api = FakeApi({t["id"]: t for t in (self.songA, self.songB, self.songC)})
        self.db = TempDatabase(self.tmp.name, self.api)

        # Live: Song A twice back-to-back, then Song B. played_at is (about) when each play ended.
        self.aEnd1 = BASE + DURATION_S
        self.aEnd2 = BASE + 2 * DURATION_S
        self.bEnd = BASE + 3 * DURATION_S
        self.recent = [   #< API order: newest first
            recentlyPlayedItem(self.songB, self.bEnd),
            recentlyPlayedItem(self.songA, self.aEnd2),
            recentlyPlayedItem(self.songA, self.aEnd1),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return self.db._loadEntries()

    def testListenerFlagsPlaysAsEstimatedWithStartTimes(self):
        self.db._addToDatabaseFromListener(self.recent)
        entries = sorted(self.entries(), key=lambda e: e["playedAt"])
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e["estimated"] for e in entries))
        self.assertTrue(all(e["timePlayed"] == DURATION_MS for e in entries))
        self.assertEqual([e["playedAt"] for e in entries], [BASE, BASE + DURATION_S, BASE + 2 * DURATION_S])

    def testListenerPollingTwiceDoesNotDuplicate(self):
        self.db._addToDatabaseFromListener(self.recent)
        self.db._addToDatabaseFromListener(self.recent)
        self.assertEqual(len(self.entries()), 3)

    def testLocalFilesAreIgnored(self):
        local = dict(self.songA, id=None, is_local=True)
        self.db._addToDatabaseFromListener([recentlyPlayedItem(local, BASE)])
        self.assertEqual(self.entries(), [])

    def testExtendedImportReplacesLivePlaysInsteadOfDoubling(self):
        self.db._addToDatabaseFromListener(self.recent)

        # The export's timestamps differ from the live ones: a few seconds of clock skew, and the
        # repeat was only listened to for 60s, so its real start is ~2m20s after the estimate
        # (estimate = played_at - full track length). Also a 5s skip and an older play of Song C.
        export = [
            extendedRow(self.songC, BASE - 3600, DURATION_MS),
            extendedRow(self.songA, BASE - 10, 5_000, skipped=True),
            extendedRow(self.songA, self.aEnd1 + 4, DURATION_MS),
            extendedRow(self.songA, self.aEnd2 + 3, 60_000, skipped=True),
        ]
        self.db.importHistory(json.dumps(export))

        entries = sorted(self.entries(), key=lambda e: e["playedAt"])
        songAPlays = [e for e in entries if e["id"] == "trackA"]
        self.assertEqual(len(entries), 5, entries)            #< C, A skip, A full, A partial, B (live)
        self.assertEqual(len(songAPlays), 3)
        self.assertFalse(any(e.get("estimated") for e in songAPlays))
        self.assertEqual([e["timePlayed"] for e in songAPlays], [5_000, DURATION_MS, 60_000])
        songB = [e for e in entries if e["id"] == "trackB"]
        self.assertEqual(len(songB), 1)
        self.assertTrue(songB[0]["estimated"])                #< not in the export, so it stays
        progress = self.db.readProgress()
        self.assertIn("2 live-recorded play(s) replaced", progress["message"])

        # Re-importing the same file and re-polling the listener change nothing.
        self.db.importHistory(json.dumps(export))
        self.db._addToDatabaseFromListener(self.recent)
        self.assertEqual(len(self.entries()), 5)
        self.assertIn("already in the database", self.db.readProgress()["message"])

    def testFailedImportSavesNoPlays(self):
        self.db._addToDatabaseFromListener(self.recent)
        self.api.client.track.side_effect = RateLimitedError(time.time() + 3600)
        export = [extendedRow(self.songC, BASE - 3600, DURATION_MS)]
        with self.assertRaises(RateLimitedError):
            self.db.importHistory(json.dumps(export))
        self.assertEqual(len(self.entries()), 3)
        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertIn("rate limit", progress["message"])


class EstimatedStatsTests(unittest.TestCase):
    tracks = {"t1": {"id": "t1", "name": "One", "duration": 200_000, "artists": [{"name": "A"}]}}

    def testCompletionAndSkipsIgnoreEstimatedPlays(self):
        entries = [
            {"id": "t1", "playedAt": 1, "timePlayed": 50_000, "skipped": True},
            {"id": "t1", "playedAt": 2, "timePlayed": 200_000, "estimated": True},
            {"id": "t1", "playedAt": 3, "timePlayed": 200_000, "estimated": True},
        ]
        self.assertEqual(analytics.completionRate(entries, self.tracks, "t1"), 25.0)
        ranking = analytics.completionUniverse(entries, self.tracks)
        self.assertEqual(ranking[0]["plays"], 1)
        skips = analytics.skipStats(entries, self.tracks, minPlays=1)
        self.assertEqual(skips["mostSkipped"][0]["plays"], 1)
        self.assertEqual(skips["mostSkipped"][0]["skipRate"], 100.0)

    def testOnlyEstimatedPlaysHaveNoCompletion(self):
        entries = [{"id": "t1", "playedAt": 2, "timePlayed": 200_000, "estimated": True}]
        self.assertIsNone(analytics.completionRate(entries, self.tracks, "t1"))
        self.assertFalse(analytics.skipStats(entries, self.tracks, minPlays=1)["available"])


class MatchingTests(unittest.TestCase):
    def testRepeatsPairInOrder(self):
        # Estimates shifted a whole track length early still pair 1:1 with the real plays.
        real = [(100 + 200, 200, "r1"), (100 + 400, 200, "r2")]
        estimates = [(100, 200, "e1"), (100 + 200, 200, "e2")]
        self.assertEqual(playMatching.matchInOrder(real, estimates), [("r1", "e1"), ("r2", "e2")])

    def testFarApartPlaysDoNotPair(self):
        self.assertEqual(playMatching.matchInOrder([(0, 200, "r")], [(1000, 200, "e")]), [])


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = SpotifyApi(Path(self.tmp.name) / "token", "id", "secret", "https://host/callback", maxRetryWait=5)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def tooMany(seconds):
        return SpotifyException(429, -1, "rate limited", headers={"Retry-After": str(seconds)})

    def testShortRetryAfterIsWaitedOutAndReported(self):
        responses = [self.tooMany(1), {"ok": True}]

        def fn(client):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        waits = []
        self.assertEqual(self.api.call(fn, maxWait=5, onWait=waits.append), {"ok": True})
        self.assertTrue(waits)

    def testLongRetryAfterFailsFastAndBlocksOtherCallers(self):
        def fn(client):
            raise self.tooMany(7200)

        started = time.time()
        with self.assertRaises(RateLimitedError):
            self.api.call(fn, maxWait=5)
        self.assertLess(time.time() - started, 1)
        # Web/listener callers (maxWait=0) don't even try while the cooldown runs.
        never = MagicMock()
        with self.assertRaises(RateLimitedError):
            self.api.call(never, maxWait=0)
        never.assert_not_called()
        self.assertGreater(self.api.status()["rateLimitedFor"], 7000)


class RelogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = SpotifyApi(Path(self.tmp.name) / "token", "id", "secret", "https://host/callback")

    def tearDown(self):
        self.tmp.cleanup()

    def testNoTokenMeansNotConnected(self):
        self.assertFalse(self.api.isConnected())
        with self.assertRaises(SpotifyAuthError):
            self.api.accessToken()
        self.assertFalse(self.api.needsRelog())

    def testInvalidGrantAsksForRelogInsteadOfCrashing(self):
        self.api.cacheHandler.save_token_to_cache({
            "access_token": "old", "refresh_token": "dead", "expires_at": 0,
            "scope": "user-read-recently-played playlist-read-private",
        })
        self.assertTrue(self.api.isConnected())
        self.api.oauth.refresh_access_token = MagicMock(
            side_effect=SpotifyOauthError("bad", error="invalid_grant", error_description="Refresh token revoked"))

        self.api.checkToken()   #< what the background login loop calls; must not raise

        self.assertTrue(self.api.needsRelog())
        self.assertFalse(self.api.isConnected())


class LoginPageTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from app import SpotifyDashboardApp
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        dashboard = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dashboard.app = Flask("app", template_folder=str(root / "templates"), static_folder=str(root / "static"))
        dashboard.spotify = SpotifyApi(Path(self.tmp.name) / "token", "id", "secret", "https://host/callback")
        dashboard.pendingOAuthStates = {}
        dashboard.database = MagicMock(listener=None)
        dashboard.registerRoutes()
        self.dashboard = dashboard
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def testRelogPromptRenders(self):
        self.dashboard.spotify.authProblem = "expired"
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn("Relog to Spotify", html)
        self.assertIn('action="/login/start"', html)

    def testStartRedirectsToSpotifyWithState(self):
        response = self.client.post("/login/start", data={"next": "/history"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.spotify.com/authorize", response.location)
        self.assertEqual(len(self.dashboard.pendingOAuthStates), 1)

    def testCallbackRejectsUnknownState(self):
        html = self.client.get("/callback?code=abc&state=forged").get_data(as_text=True)
        self.assertIn("Login link expired", html)
        self.assertFalse(self.dashboard.spotify.isConnected())


if __name__ == "__main__":
    unittest.main()
