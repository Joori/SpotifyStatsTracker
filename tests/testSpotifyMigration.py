import datetime
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
from Database.spotifyApi import SpotifyApi, RateLimitedError, SpotifyAuthError, SCOPES
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


class MixApi(FakeApi):
    """ FakeApi whose playlist lookups 404, like Spotify-made playlists in Dev Mode. """
    def __init__(self, tracksById):
        super().__init__(tracksById)
        self.playlistCalls = 0

    def playlistName(self, playlistId):
        self.playlistCalls += 1
        raise SpotifyException(404, -1, "Resource not found")


class LiveTestFindingsTests(unittest.TestCase):
    """ Cases from a live run: export first, then the listener's first poll. """
    MIX = "37i9dQZF1EIebRCzNzc6XZ"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.girl = rawTrack("girlLikeMe", "A Girl Like Me - Alex K Mix", artist="Various Artists")
        self.girl["duration_ms"] = 227_648
        self.acid = rawTrack("powerOfAcid", "Power of Acid", artist="Ranji")
        self.acid["duration_ms"] = 548_436
        self.ufo = rawTrack("ufo", "Talking With U.F.O's - Ben Nicky Radio Edit", artist="Vini Vici")
        self.api = MixApi({t["id"]: t for t in (self.girl, self.acid, self.ufo)})
        self.db = TempDatabase(self.tmp.name, self.api)
        self.girlEnd = BASE
        self.acidEnd = BASE + 2481     #< 41 min later; this play lasted only 8.7s
        self.export = [
            extendedRow(self.girl, self.girlEnd, 227_648),
            extendedRow(self.acid, self.acidEnd, 8_714),
        ]
        mix = {"uri": f"spotify:playlist:{self.MIX}"}
        self.recent = [   #< recently-played reports the same two plays (played_at = when they ended)
            dict(recentlyPlayedItem(self.acid, self.acidEnd), context={"uri": "spotify:playlist:5tNJ1RhtIm1kI7EMiJMDOr"}),
            dict(recentlyPlayedItem(self.girl, self.girlEnd), context=mix),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        return self.db._loadEntries()

    def testListenerSkipsPlaysAlreadyImportedIncludingShortOnes(self):
        self.db.importHistory(json.dumps(self.export))
        self.db._addToDatabaseFromListener(self.recent)
        self.assertEqual(len(self.entries()), 2, self.entries())
        self.assertFalse(any(e.get("estimated") for e in self.entries()))

    def testListenerMatchesByTrackIdEvenIfStoredMetadataIsStale(self):
        self.db.importHistory(json.dumps(self.export))
        tracks = self.db._loadTracks()
        tracks["girlLikeMe"]["artists"][0]["name"] = "Marianne"   #< older metadata with a different first artist
        self.db._saveTracks(tracks)
        self.db._addToDatabaseFromListener(self.recent)
        self.assertEqual(len(self.entries()), 2, self.entries())

    def testImportReplacesShortLivePlay(self):
        self.db._addToDatabaseFromListener(self.recent)
        self.db.importHistory(json.dumps(self.export))
        entries = self.entries()
        self.assertEqual(len(entries), 2, entries)
        self.assertEqual(sorted(e["timePlayed"] for e in entries), [8_714, 227_648])
        self.assertIn("2 live-recorded play(s) replaced", self.db.readProgress()["message"])

    def testFailedMixLookupStillStoresPlayAndIsNotRetried(self):
        ufoPlay = dict(recentlyPlayedItem(self.ufo, BASE + 4000), context={"uri": f"spotify:playlist:{self.MIX}"})
        self.db._addToDatabaseFromListener([ufoPlay])
        self.assertEqual([e["id"] for e in self.entries()], ["ufo"])
        self.assertEqual(self.db.playlistName(f"playlist:{self.MIX}"), "Spotify mix")

        later = dict(recentlyPlayedItem(self.girl, BASE + 5000), context={"uri": f"spotify:playlist:{self.MIX}"})
        self.db._addToDatabaseFromListener([later, ufoPlay])
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(self.api.playlistCalls, 1)   #< 404 cached, no retry

    def testPlayIsStoredEvenIfImageAndNameLookupsCrash(self):
        def boom(*args, **kwargs):
            raise RuntimeError("network down")
        self.db.saveImagesFromTrack = boom
        self.db.updatePlaylists = boom
        self.db._addToDatabaseFromListener([recentlyPlayedItem(self.ufo, BASE + 4000)])
        self.assertEqual([e["id"] for e in self.entries()], ["ufo"])

    def testDataVersionChangesOnlyWhenPlaysAreSaved(self):
        before = self.db.dataVersion
        self.db._addToDatabaseFromListener(self.recent)
        afterFirst = self.db.dataVersion
        self.assertNotEqual(before, afterFirst)
        self.db._addToDatabaseFromListener(self.recent)   #< nothing new
        self.assertEqual(afterFirst, self.db.dataVersion)

    def testCleanupRemovesOnlyDuplicateLivePlays(self):
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tools')))
        import removeDuplicateLivePlays as cleanup

        # The live run's state: export imported, then duplicates added by the old listener,
        # plus one live play that isn't in the export and must stay.
        self.db.importHistory(json.dumps(self.export))
        entries = self.entries()
        for track, end in ((self.girl, self.girlEnd), (self.acid, self.acidEnd)):
            entries.append({"id": track["id"], "playedAt": end - track["duration_ms"] // 1000,
                            "timePlayed": track["duration_ms"], "playedFrom": None, "estimated": True})
        self.db._saveTracks(dict(self.db._loadTracks(), ufo={**self.db._loadTracks()["girlLikeMe"], "id": "ufo", "name": "UFO"}))
        entries.append({"id": "ufo", "playedAt": BASE + 9000, "timePlayed": 1, "playedFrom": None, "estimated": True})
        self.db._saveEntries(entries)

        lines = []
        found = cleanup.run(self.tmp.name, apply=False, out=lines.append)
        self.assertEqual(sorted(e["id"] for e in found), ["girlLikeMe", "powerOfAcid"])
        self.assertEqual(len(json.loads(Path(self.db.entriesPath).read_text())), 5)   #< dry run writes nothing
        self.assertIn("Dry run", lines[-1])

        cleanup.run(self.tmp.name, apply=True, out=lines.append)
        kept = json.loads(Path(self.db.entriesPath).read_text())
        self.assertEqual(len(kept), 3)
        self.assertEqual([e["id"] for e in kept if e.get("estimated")], ["ufo"])
        self.assertTrue(list(Path(self.tmp.name).glob("entries.json.bak-*")))


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
            "scope": SCOPES,
        })
        self.assertTrue(self.api.isConnected())
        self.api.oauth.refresh_access_token = MagicMock(
            side_effect=SpotifyOauthError("bad", error="invalid_grant", error_description="Refresh token revoked"))

        self.api.checkToken()   #< what the background login loop calls; must not raise

        self.assertTrue(self.api.needsRelog())
        self.assertFalse(self.api.isConnected())

    def testTokenMissingNewScopesAsksForRelogWithoutNetwork(self):
        self.api.cacheHandler.save_token_to_cache({
            "access_token": "ok", "refresh_token": "ok", "expires_at": time.time() + 3600,
            "scope": "user-read-recently-played playlist-read-private",
        })
        self.api.oauth.refresh_access_token = MagicMock()

        self.assertIn("user-top-read", self.api.cachedMissingScopes())
        self.assertFalse(self.api.isConnected())
        self.assertTrue(self.api.needsRelog())
        with self.assertRaises(SpotifyAuthError):
            self.api.accessToken()
        self.api.oauth.refresh_access_token.assert_not_called()

    def testScopeOrderDoesNotMatter(self):
        self.api.cacheHandler.save_token_to_cache({
            "access_token": "ok", "refresh_token": "ok", "expires_at": time.time() + 3600,
            "scope": " ".join(reversed(SCOPES.split())),
        })
        self.assertTrue(self.api.isConnected())
        self.assertFalse(self.api.needsRelog())
        self.assertEqual(self.api.accessToken(), "ok")


class SummaryApiTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from app import SpotifyDashboardApp
        from Database.Formatters.spotifyClient import Client
        from Database.Listeners.spotifyListener import Listener
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        songA, songB = rawTrack("trackA", "Song A"), rawTrack("trackB", "Song B", artist="Artist Two")
        api = FakeApi({t["id"]: t for t in (songA, songB)})
        api.recentlyPlayed = lambda limit=50: []
        self.db = TempDatabase(self.tmp.name, api)

        nowTs = int(time.time())
        self.db._saveTracks({t["id"]: Client.formatTrack(t, embedPlaybackInfo=False) for t in (songA, songB)})
        self.db._saveEntries([
            {"id": "trackA", "playedAt": nowTs - 20 * 86400, "timePlayed": 100_000, "playedFrom": None},
            {"id": "trackA", "playedAt": nowTs - 3 * 86400, "timePlayed": 120_000, "playedFrom": None},
            {"id": "trackB", "playedAt": nowTs - 600, "timePlayed": DURATION_MS, "playedFrom": None, "estimated": True},
            {"id": "trackA", "playedAt": nowTs - 3600, "timePlayed": 90_500, "playedFrom": None},   #< out of order on purpose
        ])
        self.db.listener = Listener(api)
        self.db.listener.pollOnce(lambda items: None)

        dashboard = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dashboard.app = Flask("app", template_folder=str(root / "templates"), static_folder=str(root / "static"))
        dashboard.spotify = SpotifyApi(Path(self.tmp.name) / "token", "id", "secret", "https://host/callback")
        dashboard.spotify.isConnected = lambda: True
        dashboard.pendingOAuthStates = {}
        dashboard.database = self.db
        dashboard.registerRoutes()
        self.dashboard = dashboard
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def testSummaryMatchesOverviewNumbers(self):
        response = self.client.get("/api/summary")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        self.assertEqual(data["plays_today"], 2)
        self.assertEqual(data["listen_seconds_today"], (DURATION_MS + 90_500) // 1000)
        self.assertEqual(data["plays_week"], 3)
        self.assertEqual(data["total_plays"], 4)
        # Same functions and ranges the Overview page uses
        for interval, plays in (("day", "plays_today"), ("week", "plays_week")):
            start, end = self.dashboard._getDateRange(interval)
            self.assertEqual(data[plays], self.db.getOverallStats(start, end)["totalSongsPlayed"])
        self.assertEqual(data["current_streak_days"], self.db.getStreakStats()["currentStreak"])
        self.assertGreaterEqual(data["current_streak_days"], 1)

        self.assertEqual(data["last_played"]["track"], "Song B")
        self.assertEqual(data["last_played"]["artist"], "Artist Two")
        self.assertTrue(data["listener"]["connected"])
        for stamp in (data["last_played"]["played_at"], data["listener"]["last_poll"]):
            self.assertIsNotNone(datetime.datetime.fromisoformat(stamp).tzinfo, stamp)

        self.assertEqual(set(data), {"plays_today", "listen_seconds_today", "plays_week", "total_plays",
                                     "current_streak_days", "last_played", "listener", "now_playing"})
        self.assertIsNone(data["now_playing"])   #< no now-playing store in this setup
        body = response.get_data(as_text=True).lower()
        for secret in ("secret", "token", "client_id", "access", "refresh"):
            self.assertNotIn(secret, body)

    def testSummaryWorksWithNoPlaysAndNoListener(self):
        self.db._saveEntries([])
        self.db.listener = None
        self.dashboard.spotify.isConnected = lambda: False
        data = self.client.get("/api/summary").get_json()
        self.assertEqual((data["plays_today"], data["total_plays"], data["current_streak_days"]), (0, 0, 0))
        self.assertIsNone(data["last_played"])
        self.assertEqual(data["listener"], {"connected": False, "last_poll": None})


    def testSummaryIncludesNowPlayingWithoutSecrets(self):
        from Database.nowPlaying import NowPlaying
        api = FakeApi({})
        api.call = lambda fn, maxWait=0, onWait=None: {
            "currently_playing_type": "track", "is_playing": True, "progress_ms": 1000,
            "item": {"name": "Song B", "duration_ms": DURATION_MS, "artists": [{"name": "Artist Two"}],
                     "album": {"name": "Album", "images": []}}}
        self.dashboard.nowPlaying = NowPlaying(api)
        data = self.client.get("/api/summary").get_json()
        self.assertEqual(data["now_playing"], {"is_playing": True, "type": "track", "track": "Song B",
                                               "artist": "Artist Two", "album": "Album",
                                               "progress_ms": 1000, "duration_ms": DURATION_MS})

    def testOverviewHasNowPlayingCard(self):
        self.dashboard.username = "test"
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="now-playing"', html)
        self.assertIn("js/now-playing.js", html)


    def testInsightsShowsSpotifyVsYou(self):
        from Database.spotifyTop import SpotifyTopStore
        self.dashboard.username = "test"
        store = SpotifyTopStore(self.db.spotify, Path(self.tmp.name))
        store.cache.save({
            "tracks": {r: [{"id": "spotifyOnly", "name": "Spotify Darling", "artists": [{"id": "x", "name": "X"}],
                            "isrc": None, "imageUrl": "https://i.scdn.co/c", "url": "https://open.spotify.com/track/s"}]
                       for r in ("short_term", "medium_term", "long_term")},
            "artists": {r: [] for r in ("short_term", "medium_term", "long_term")},
        })
        self.dashboard.spotifyTop = store
        html = self.client.get("/insights").get_data(as_text=True)
        self.assertIn('id="spotify-vs-you"', html)
        self.assertIn("Spotify Darling", html)
        self.assertIn("no plays here", html)
        self.assertIn("Song A", html)   #< your most played, not in Spotify's list
        self.assertIn("Spotify returned no top artists", html)
        self.assertIn("The tracker counted 4 plays in the last year", html)

    def testInsightsShowsLikedVsPlayed(self):
        from Database.likedSongs import LikedStore
        self.dashboard.username = "test"
        store = LikedStore(self.db.spotify, Path(self.tmp.name))
        store.cache.save({"tracks": [
            {"id": "trackB", "name": "Song B", "isrc": None, "artists": [{"id": "artist-1", "name": "Artist Two"}],
             "imageUrl": None, "url": None, "addedAt": "2026-01-02T00:00:00Z"},
            {"id": "neverHere", "name": "Liked Ghost", "isrc": None, "artists": [{"id": "z", "name": "Z"}],
             "imageUrl": "https://i.scdn.co/g", "url": "https://open.spotify.com/track/g", "addedAt": "2026-03-04T00:00:00Z"},
        ], "total": 2})
        self.dashboard.likedSongs = store
        html = self.client.get("/insights").get_data(as_text=True)
        self.assertIn('id="liked-vs-played"', html)
        self.assertIn("Liked Ghost", html)
        self.assertIn("Never played · liked 2026-03-04", html)
        self.assertIn("1 play · liked 2026-01-02", html)
        self.assertIn("Everything you play 10 or more times is liked.", html)

    def testInsightsSurvivesSpotifyVsYouFailure(self):
        self.dashboard.username = "test"
        self.dashboard.spotifyTop = MagicMock()
        self.dashboard.spotifyTop.comparison.side_effect = RuntimeError("boom")
        response = self.client.get("/insights")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Couldn't get your top lists", response.get_data(as_text=True))


class PlaylistOverrideTests(unittest.TestCase):
    MIX = "37i9dQZF1EIebRCzNzc6XZ"

    def setUp(self):
        from flask import Flask
        from app import SpotifyDashboardApp
        from Database.Formatters.spotifyClient import Client
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        songA, songB = rawTrack("trackA", "Song A"), rawTrack("trackB", "Song B")
        self.db = TempDatabase(self.tmp.name, FakeApi({}))
        self.db.playlistOverridesPath = Path(self.tmp.name) / "playlist_overrides.json"
        self.db.imgDir_playlists = Path(self.tmp.name) / "img" / "playlists"
        self.db._saveTracks({t["id"]: Client.formatTrack(t, embedPlaybackInfo=False) for t in (songA, songB)})
        # Lookups failed before overrides existed: the mix was cached as None, like on a live install
        self.db._savePlaylists({"album": {}, "playlist": {self.MIX: None, "mine": "My Playlist"}})
        mix = f"playlist:{self.MIX}"
        self.db._saveEntries([
            {"id": "trackA", "playedAt": BASE, "timePlayed": 1000, "playedFrom": mix},
            {"id": "trackB", "playedAt": BASE + 10, "timePlayed": 1000, "playedFrom": mix},
            {"id": "trackB", "playedAt": BASE + 20, "timePlayed": 1000, "playedFrom": mix},
            {"id": "trackA", "playedAt": BASE + 30, "timePlayed": 1000, "playedFrom": "playlist:mine"},
        ])

        dashboard = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dashboard.app = Flask("app", template_folder=str(root / "templates"), static_folder=str(root / "static"))
        dashboard.spotify = SpotifyApi(Path(self.tmp.name) / "token", "id", "secret", "https://host/callback")
        dashboard.spotify.isConnected = lambda: True
        dashboard.pendingOAuthStates = {}
        dashboard.username = "test"
        dashboard.database = self.db
        dashboard.registerRoutes()
        self.dashboard = dashboard
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def writeOverrides(self, data):
        self.db.playlistOverridesPath.write_text(json.dumps(data))
        stamp = time.time() + len(json.dumps(data))   #< distinct mtime per write
        os.utime(self.db.playlistOverridesPath, (stamp, stamp))

    def contexts(self):
        return {c["id"]: c for c in self.db.getContextsStats()}

    def testWithoutOverridesMixUsesMostPlayedTrackCover(self):
        mix = self.contexts()[f"playlist:{self.MIX}"]
        self.assertEqual(mix["name"], "Spotify mix")
        self.assertEqual(mix["imageId"], "album-trackB")   #< Song B was played most from it
        self.assertIsNone(mix["coverFile"])

    def testOverrideNameAndCoverWinWithoutReimporting(self):
        self.db.imgDir_playlists.mkdir(parents=True)
        (self.db.imgDir_playlists / "mix.png").write_bytes(b"png")
        self.writeOverrides({self.MIX: {"name": "Daily Mix 1", "cover": "mix.png"},
                             "spotify:playlist:mine": {"name": "Renamed"}})
        contexts = self.contexts()
        self.assertEqual(contexts[f"playlist:{self.MIX}"]["name"], "Daily Mix 1")
        self.assertEqual(contexts[f"playlist:{self.MIX}"]["coverFile"], "mix.png")
        self.assertEqual(contexts["playlist:mine"]["name"], "Renamed")   #< beats the API name too

        self.writeOverrides({self.MIX: {"name": "Edited again"}})   #< picked up without a restart
        self.assertEqual(self.db.playlistName(f"playlist:{self.MIX}"), "Edited again")

    def testBadCoverEntriesAreIgnored(self):
        for cover in ("missing.png", "../entries.json", "", 5):
            self.writeOverrides({self.MIX: {"cover": cover}})
            self.assertIsNone(self.db.playlistCoverFile(f"playlist:{self.MIX}"), cover)
        self.db.playlistOverridesPath.write_text("{not json")
        os.utime(self.db.playlistOverridesPath, (time.time() + 999, time.time() + 999))
        self.assertEqual(self.db.playlistName(f"playlist:{self.MIX}"), "Spotify mix")

    def testExploreAndPlaylistPageNeverRenderAnEmptyCover(self):
        self.db._saveTracks({})   #< no track info at all: only the placeholder is left
        for url in ("/explore?tab=contexts", f"/playlist/{self.MIX}"):
            html = self.client.get(url).get_data(as_text=True)
            self.assertIn("/static/images/playlist-placeholder.svg", html, url)
            self.assertNotIn("/tracks/.jpeg", html, url)
            self.assertNotIn("/tracks/None.jpeg", html, url)

    def testExploreShowsOverrideCover(self):
        self.db.imgDir_playlists.mkdir(parents=True)
        (self.db.imgDir_playlists / "mix.png").write_bytes(b"png")
        self.writeOverrides({self.MIX: {"cover": "mix.png"}})
        html = self.client.get("/explore?tab=contexts").get_data(as_text=True)
        self.assertIn('src="/img/test/playlists/mix.png"', html)
        self.assertIn('src="/img/test/tracks/album-trackA.jpeg"', html)   #< the other playlist: its top track


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

    def testMissingScopesShowNewPermissionsPrompt(self):
        self.dashboard.spotify.cacheHandler.save_token_to_cache({
            "access_token": "ok", "refresh_token": "ok", "expires_at": time.time() + 3600,
            "scope": "user-read-recently-played playlist-read-private",
        })
        html = self.client.get("/login").get_data(as_text=True)
        self.assertIn("Relog to Spotify", html)
        self.assertIn("new Spotify permissions", html)

    def testStartRedirectsToSpotifyWithState(self):
        response = self.client.post("/login/start", data={"next": "/history"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.spotify.com/authorize", response.location)
        for scope in SCOPES.split():
            self.assertIn(scope, response.location)
        self.assertEqual(len(self.dashboard.pendingOAuthStates), 1)

    def testCallbackRejectsUnknownState(self):
        html = self.client.get("/callback?code=abc&state=forged").get_data(as_text=True)
        self.assertIn("Login link expired", html)
        self.assertFalse(self.dashboard.spotify.isConnected())


if __name__ == "__main__":
    unittest.main()
