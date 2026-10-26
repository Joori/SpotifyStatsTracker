import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Database import playlistGenerator as pg
from Database.spotifyApi import RateLimitedError
from spotipy.exceptions import SpotifyException

NOW = datetime.datetime(2026, 10, 26, 12, 0).astimezone()


def tid(n):
    return f"{n:0>22}"[-22:].replace("0", "a")   #< 22 base62 chars, like a Spotify ID


def ts(year, month=6, day=15, hour=12):
    return datetime.datetime(year, month, day, hour).astimezone().timestamp()


def tracksFor(ids):
    return {i: {"id": i, "name": f"Song {i[-3:]}", "artists": [{"id": "a", "name": "A"}], "imageId": None} for i in ids}


class GenerateTests(unittest.TestCase):
    def testTopYearCountsOnlyThatYearAndCapsAt50(self):
        entries = []
        for n in range(60):
            entries += [{"id": tid(n), "playedAt": ts(2020)}] * (n + 1)
        entries += [{"id": tid(999), "playedAt": ts(2021)}] * 500   #< other year: ignored
        result = pg.generate("top-year", {"year": "2020"}, entries, tracksFor([tid(n) for n in range(60)] + [tid(999)]), now=NOW)
        self.assertEqual((result["key"], result["name"]), ("top-year-2020", "Top 50 of 2020"))
        self.assertEqual(len(result["tracks"]), 50)
        self.assertEqual(result["tracks"][0], {"id": tid(59), "detail": "60 plays in 2020"})
        self.assertIn(pg.MARKER, result["description"])

    def testYearBoundariesUseLocalTime(self):
        entries = [{"id": tid(1), "playedAt": ts(2020, 12, 31, 23)}, {"id": tid(2), "playedAt": ts(2021, 1, 1, 0)}]
        result = pg.generate("top-year", {"year": 2020}, entries, tracksFor([tid(1), tid(2)]), now=NOW)
        self.assertEqual([t["id"] for t in result["tracks"]], [tid(1)])

    def testForgottenFavourites(self):
        old, recent = ts(2022), (NOW - datetime.timedelta(days=30)).timestamp()
        entries = ([{"id": tid(1), "playedAt": old}] * 20                                     #< forgotten
                   + [{"id": tid(2), "playedAt": old}] * 15 + [{"id": tid(2), "playedAt": recent}] * 2   #< 2 recent: still forgotten
                   + [{"id": tid(3), "playedAt": old}] * 30 + [{"id": tid(3), "playedAt": recent}] * 3   #< still played
                   + [{"id": tid(4), "playedAt": old}] * 9)                                     #< never a favourite
        tracks = tracksFor([tid(n) for n in range(1, 5)])
        result = pg.generate("forgotten", {}, entries, tracks, now=NOW)
        self.assertEqual([t["id"] for t in result["tracks"]], [tid(1), tid(2)])
        self.assertEqual(result["tracks"][0]["detail"], "20 plays before the last 12 months · none in the last 12 months")
        self.assertEqual(result["key"], "forgotten")

        byYear = pg.generate("forgotten", {"year": "2022"}, entries, tracks, now=NOW)
        self.assertEqual((byYear["key"], byYear["name"]), ("forgotten-2022", "Forgotten favourites (2022)"))
        self.assertEqual(pg.generate("forgotten", {"year": "2021"}, entries, tracks, now=NOW)["tracks"], [])
        with self.assertRaises(pg.GeneratorError):
            pg.generate("forgotten", {"year": "2026"}, entries, tracks, now=NOW)

    def testPeakObsessionsKeepsEachSongsBestDayOnce(self):
        entries = ([{"id": tid(1), "playedAt": ts(2020, 1, 1)}] * 8 + [{"id": tid(1), "playedAt": ts(2020, 2, 1)}] * 5
                   + [{"id": tid(2), "playedAt": ts(2021, 3, 3)}] * 6)
        result = pg.generate("peak-obsessions", {}, entries, tracksFor([tid(1), tid(2)]), now=NOW)
        self.assertEqual([t["id"] for t in result["tracks"]], [tid(1), tid(2)])
        self.assertTrue(result["tracks"][0]["detail"].startswith("8 plays on "))

    def testSkipsIdsThatArentSpotifyTracks(self):
        entries = [{"id": "local-musicolet-1", "playedAt": ts(2020)}] * 5 + [{"id": tid(1), "playedAt": ts(2020)}]
        tracks = dict(tracksFor([tid(1)]), **{"local-musicolet-1": {"name": "Local"}})
        result = pg.generate("top-year", {"year": "2020"}, entries, tracks, now=NOW)
        self.assertEqual([t["id"] for t in result["tracks"]], [tid(1)])

    def testBadParameters(self):
        for kind, params in (("top-year", {}), ("top-year", {"year": "abc"}), ("nope", {})):
            with self.assertRaises(pg.GeneratorError):
                pg.generate(kind, params, [], {}, now=NOW)


class FakeSpotify:
    """ Just enough playlist state to test create vs update. """
    def __init__(self, me="me1"):
        self.me = me
        self.playlists = {}      #< id -> playlist dict (in the user's library)
        self.requests = []
        self.failNext = None
        self.connected = True
        self.unplayable = {}      #< track id -> restrictions reason (None = is_playable false, no reason)
        self.missing = set()      #< track ids Spotify answers 404 for
        self.forcePublic = False  #< simulate Spotify keeping a playlist public

    def isConnected(self):
        return self.connected

    def call(self, fn, maxWait=0, onWait=None):
        client = MagicMock()
        client._get.side_effect = self._get
        client._post.side_effect = self._post
        client._put.side_effect = self._put
        return fn(client)

    def _maybeFail(self, url):
        if self.failNext and self.failNext[0] == url:
            error, self.failNext = self.failNext[1], None
            raise error

    def _get(self, url, **kw):
        self.requests.append(("GET", url, kw))
        if url == "me":
            return {"id": self.me}
        if url == "me/playlists":
            items = list(self.playlists.values())[kw["offset"]:kw["offset"] + kw["limit"]]
            more = kw["offset"] + kw["limit"] < len(self.playlists)
            return {"items": items, "next": "x" if more else None}
        if url.startswith("tracks/"):
            trackId = url.split("/")[1]
            assert kw.get("market") == "from_token", kw
            if trackId in self.missing:
                raise SpotifyException(404, -1, "Not found")
            if trackId in self.unplayable:
                reason = self.unplayable[trackId]
                return {"id": trackId, "is_playable": False, **({"restrictions": {"reason": reason}} if reason else {})}
            return {"id": trackId, "is_playable": True}
        if url.startswith("playlists/") and kw.get("fields") == "public":
            return {"public": self.playlists[url.split("/")[1]]["public"]}
        raise AssertionError(url)

    def _post(self, url, payload=None, **kw):
        self.requests.append(("POST", url, payload))
        self._maybeFail(url)
        if url == "me/playlists":
            self.created = getattr(self, "created", 0) + 1
            pid = f"pl{self.created}"
            self.playlists[pid] = {"id": pid, "name": payload["name"], "description": payload["description"],
                                   "public": True if self.forcePublic else payload["public"],
                                   "owner": {"id": self.me}, "uris": []}
            return {"id": pid}
        pid = url.split("/")[1]
        self.playlists[pid]["uris"] += payload["uris"]
        return {"snapshot_id": "s"}

    def _put(self, url, payload=None, **kw):
        self.requests.append(("PUT", url, payload))
        self._maybeFail(url)
        pid = url.split("/")[1]
        if url.endswith("/items"):
            self.playlists[pid]["uris"] = list(payload["uris"])
        else:
            self.playlists[pid].update(payload)
            if self.forcePublic and "public" in payload and set(payload) != {"public"}:
                self.playlists[pid]["public"] = True   #< ignores public in a details update, honours it alone
        return None

    def writes(self):
        return [(m, u) for m, u, _ in self.requests if m != "GET"]


def playlist(n=3, key="top-year-2020", name="Top 50 of 2020"):
    return {"key": key, "name": name, "description": f"Desc. {pg.MARKER}",
            "tracks": [{"id": tid(i), "detail": ""} for i in range(n)]}


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spotify = FakeSpotify()
        self.publisher = pg.PlaylistPublisher(self.spotify, Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def testCreatesPrivatePlaylistWithCurrentEndpoints(self):
        record = self.publisher.publish(playlist())
        self.assertTrue(record["created"])
        self.assertEqual(self.spotify.writes(), [("POST", "me/playlists"), ("PUT", f"playlists/{record['id']}/items")])
        made = self.spotify.playlists[record["id"]]
        self.assertFalse(made["public"])
        self.assertEqual(made["uris"], [f"spotify:track:{tid(i)}" for i in range(3)])
        self.assertEqual(self.publisher.existing("top-year-2020")["url"], f"https://open.spotify.com/playlist/{record['id']}")
        for _, url, _ in self.spotify.requests:   #< never the deprecated endpoints
            self.assertFalse(url.startswith("users/") or url.endswith("/tracks"), url)

    def testRerunUpdatesTheSamePlaylist(self):
        first = self.publisher.publish(playlist(3))
        self.spotify.requests.clear()
        second = self.publisher.publish(playlist(2))
        self.assertFalse(second["created"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.spotify.playlists), 1)
        self.assertEqual(self.spotify.writes(), [("PUT", f"playlists/{first['id']}"), ("PUT", f"playlists/{first['id']}/items")])
        self.assertEqual(len(self.spotify.playlists[first["id"]]["uris"]), 2)

    def testRemovedFromLibraryMakesANewOne(self):
        first = self.publisher.publish(playlist())
        del self.spotify.playlists[first["id"]]
        second = self.publisher.publish(playlist())
        self.assertTrue(second["created"])
        self.assertNotEqual(second["id"], first["id"])

    def testLostRecordReusesOurPlaylistButNeverAHandMadeOne(self):
        self.spotify.playlists["mine"] = {"id": "mine", "name": "Top 50 of 2020", "description": "my own list",
                                          "owner": {"id": "me1"}, "uris": []}
        self.spotify.playlists["other"] = {"id": "other", "name": "Top 50 of 2020", "description": pg.MARKER,
                                           "owner": {"id": "someone-else"}, "uris": []}
        made = self.publisher.publish(playlist())
        self.assertTrue(made["created"])

        (Path(self.tmp.name) / "generated_playlists.json").unlink()
        self.publisher = pg.PlaylistPublisher(self.spotify, Path(self.tmp.name))
        again = self.publisher.publish(playlist())
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], made["id"])
        self.assertEqual(self.spotify.playlists["mine"]["uris"], [])

    def testMoreThan100SongsAreSentInChunks(self):
        record = self.publisher.publish(playlist(150))
        self.assertEqual(self.spotify.writes()[1:], [("PUT", f"playlists/{record['id']}/items"),
                                                     ("POST", f"playlists/{record['id']}/items")])
        self.assertEqual(len(self.spotify.playlists[record["id"]]["uris"]), 150)

    def testFailureAfterCreateIsRetriedAsAnUpdate(self):
        self.spotify.failNext = ("playlists/pl1/items", RateLimitedError(time.time() + 60))
        with self.assertRaises(RateLimitedError):
            self.publisher.publish(playlist())
        self.assertFalse(self.publisher.existing("top-year-2020")["complete"])
        retry = self.publisher.publish(playlist())
        self.assertFalse(retry["created"])
        self.assertEqual(len(self.spotify.playlists), 1)
        self.assertTrue(self.publisher.existing("top-year-2020")["complete"])

    def testEmptyPlaylistIsRefused(self):
        with self.assertRaises(pg.GeneratorError):
            self.publisher.publish(playlist(0))
        self.assertEqual(self.spotify.requests, [])


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spotify = FakeSpotify()
        self.publisher = pg.PlaylistPublisher(self.spotify, Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def testCreateSendsJsonFalseAndReadsItBack(self):
        record = self.publisher.publish(playlist())
        create = next(p for m, u, p in self.spotify.requests if (m, u) == ("POST", "me/playlists"))
        self.assertIn('"public": false', json.dumps(create))
        self.assertIn(("GET", f"playlists/{record['id']}", {"fields": "public"}), self.spotify.requests)
        self.assertNotIn(("PUT", f"playlists/{record['id']}", {"public": False}), self.spotify.requests)   #< already private

    def testPlaylistThatComesBackPublicIsFixedWithAWarning(self):
        self.spotify.forcePublic = True
        with patch("builtins.print") as printed:
            record = self.publisher.publish(playlist())
        self.assertIn(("PUT", f"playlists/{record['id']}", {"public": False}), self.spotify.requests)
        self.assertFalse(self.spotify.playlists[record["id"]]["public"])
        self.assertTrue(any("Warning" in str(c) and "public" in str(c) for c in printed.call_args_list))

    def testUpdateAlsoKeepsItPrivate(self):
        record = self.publisher.publish(playlist())
        self.spotify.playlists[record["id"]]["public"] = True   #< e.g. changed in the app since
        self.spotify.forcePublic = True
        self.spotify.requests.clear()
        self.publisher.publish(playlist())
        details = next(p for m, u, p in self.spotify.requests if (m, u) == ("PUT", f"playlists/{record['id']}"))
        self.assertIs(details["public"], False)
        self.assertEqual(self.spotify.requests[-1], ("PUT", f"playlists/{record['id']}", {"public": False}))
        self.assertFalse(self.spotify.playlists[record["id"]]["public"])


class PlayabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spotify = FakeSpotify()
        self.checker = pg.PlayabilityChecker(self.spotify, Path(self.tmp.name), pause=0)

    def tearDown(self):
        self.tmp.cleanup()

    def lookups(self):
        return [u for m, u, _ in self.spotify.requests if u.startswith("tracks/")]

    def testRestrictionsUnplayableAndMissingTracks(self):
        self.spotify.unplayable = {tid(1): "market", tid(2): None}
        self.spotify.missing = {tid(3)}
        self.checker.checkNow([tid(i) for i in range(5)])
        self.assertEqual(self.checker.known(tid(0))["playable"], True)
        self.assertEqual((self.checker.known(tid(1))["playable"], self.checker.known(tid(1))["reason"]), (False, "market"))
        self.assertFalse(self.checker.known(tid(2))["playable"])
        self.assertEqual(self.checker.known(tid(3))["reason"], "notFound")
        self.assertEqual(len(self.lookups()), 5)

        self.checker.checkNow([tid(i) for i in range(5)])   #< cached: no new lookups
        self.assertEqual(len(self.lookups()), 5)

    def testRateLimitPausesAndKeepsTheRestQueued(self):
        self.spotify.failNext = None
        realGet = self.spotify._get
        def get(url, **kw):
            if url == f"tracks/{tid(1)}":
                raise RateLimitedError(time.time() + 600)
            return realGet(url, **kw)
        self.spotify._get = get
        self.checker.checkNow([tid(0), tid(1), tid(2)])
        self.assertIsNotNone(self.checker.known(tid(0)))
        self.assertIsNone(self.checker.known(tid(2)))
        self.assertIsNotNone(self.checker.pausedUntil())
        self.checker.ensure([tid(2)])   #< paused: doesn't start again
        self.assertFalse(self.checker._running)

    def testBackfillsFromTheNextRankedSongs(self):
        candidates = {"candidates": [{"id": tid(i), "detail": f"#{i}"} for i in range(60)]}
        self.spotify.unplayable = {tid(3): "market", tid(10): "market"}
        first = pg.selectPlayable(candidates, self.checker)
        self.assertEqual(first["pending"], 50)            #< nothing known yet: checking
        self.checker.thread.join(5)
        second = pg.selectPlayable(candidates, self.checker)
        if second["pending"]:                              #< two more were needed after the skips
            self.checker.thread.join(5)
            second = pg.selectPlayable(candidates, self.checker)
        self.assertEqual(second["pending"], 0)
        ids = [t["id"] for t in second["tracks"]]
        self.assertEqual(len(ids), 50)
        self.assertNotIn(tid(3), ids)
        self.assertEqual(ids[-2:], [tid(50), tid(51)])
        self.assertEqual([(t["id"], t["unavailable"]) for t in second["skipped"]],
                         [(tid(3), "unavailable in your region"), (tid(10), "unavailable in your region")])
        self.assertEqual(len(self.lookups()), 52)


class PlaylistMakerRouteTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from app import SpotifyDashboardApp
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        ids = [tid(n) for n in range(3)]
        entries = [{"id": i, "playedAt": ts(2020), "timePlayed": 1} for i in ids for _ in range(2)]
        dashboard = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dashboard.app = Flask("app", template_folder=str(root / "templates"), static_folder=str(root / "static"))
        dashboard.username = "test"
        dashboard.spotify = MagicMock(redirectUri="https://host/callback")
        dashboard.spotify.isConnected.return_value = True
        dashboard.pendingOAuthStates = {}
        dashboard.database = MagicMock(listener=None)
        dashboard.database._loadEntries.return_value = entries
        dashboard.database._loadTracks.return_value = tracksFor(ids)
        dashboard.database.getAvailableYears.return_value = [2020]
        self.spotifyFake = FakeSpotify()
        dashboard.playlistPublisher = pg.PlaylistPublisher(self.spotifyFake, Path(self.tmp.name))
        dashboard.playabilityChecker = pg.PlayabilityChecker(self.spotifyFake, Path(self.tmp.name), pause=0)
        dashboard.registerRoutes()
        self.ids = ids
        self.dashboard = dashboard
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def checked(self):
        self.dashboard.playabilityChecker.checkNow(self.ids)
        self.spotifyFake.requests.clear()

    def testPreviewChecksAvailabilityButChangesNothing(self):
        html = self.client.get("/playlist-maker?kind=top-year&year=2020").get_data(as_text=True)
        self.assertIn("Checking which songs are available in your region", html)
        self.assertIn("disabled", html)
        self.dashboard.playabilityChecker.thread.join(5)

        self.spotifyFake.unplayable = {}
        html = self.client.get("/playlist-maker?kind=top-year&year=2020").get_data(as_text=True)
        self.assertIn("Top 50 of 2020", html)
        self.assertIn("Create playlist on Spotify", html)
        self.assertNotIn("Checking which songs", html)
        self.assertEqual(html.count('class="insight-entity"'), 3)
        self.assertEqual(html.count('class="entity-cover"'), 3)   #< cover box even when a song has no image
        self.assertEqual(self.spotifyFake.writes(), [])

    def testUnavailableSongsAreListedAsSkipped(self):
        self.spotifyFake.unplayable = {self.ids[1]: "market"}
        self.checked()
        html = self.client.get("/playlist-maker?kind=top-year&year=2020").get_data(as_text=True)
        self.assertIn("unavailable in your region", html)
        self.assertIn("1 unavailable skipped", html)
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 302)
        uris = self.spotifyFake.playlists["pl1"]["uris"]
        self.assertEqual(len(uris), 2)
        self.assertNotIn(f"spotify:track:{self.ids[1]}", uris)

    def testCreateIsRefusedWhileStillChecking(self):
        self.dashboard.playabilityChecker.ensure = MagicMock()   #< checker never finishes
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.spotifyFake.writes(), [])

    def testPostFromAnotherSiteIsRejected(self):
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        noOrigin = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"})
        self.assertEqual(noOrigin.status_code, 403)
        self.assertEqual(self.spotifyFake.requests, [])

    def testBehindReverseProxyThePublicHostIsAccepted(self):
        self.checked()
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "https://host"})   #< SPOTIFY_REDIRECT_URI's host
        self.assertEqual(response.status_code, 302)
        forwarded = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                     headers={"Origin": "https://tracker.lan", "X-Forwarded-Host": "tracker.lan"})
        self.assertEqual(forwarded.status_code, 302)

    def testCreateThenUpdate(self):
        self.checked()
        headers = {"Origin": "http://localhost"}
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"}, headers=headers)
        self.assertEqual(response.status_code, 302)
        self.assertIn("done=created", response.location)
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("Created", html)
        self.assertIn("https://open.spotify.com/playlist/pl1", html)
        self.assertIn("Update playlist on Spotify", html)

        again = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"}, headers=headers)
        self.assertIn("done=updated", again.location)
        self.assertEqual(len(self.spotifyFake.playlists), 1)

    def testRateLimitIsShownNotRaised(self):
        self.checked()
        self.spotifyFake.failNext = ("me/playlists", RateLimitedError(time.time() + 600))
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("rate limiting", response.get_data(as_text=True))

    def testBadParametersShowAnError(self):
        html = self.client.get("/playlist-maker?kind=forgotten&year=2026").get_data(as_text=True)
        self.assertIn("more than 12 months ago", html)


if __name__ == "__main__":
    unittest.main()
