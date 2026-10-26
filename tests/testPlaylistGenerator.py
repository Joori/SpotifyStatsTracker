import datetime
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Database import playlistGenerator as pg
from Database.spotifyApi import RateLimitedError

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
        raise AssertionError(url)

    def _post(self, url, payload=None, **kw):
        self.requests.append(("POST", url, payload))
        self._maybeFail(url)
        if url == "me/playlists":
            self.created = getattr(self, "created", 0) + 1
            pid = f"pl{self.created}"
            self.playlists[pid] = {"id": pid, "name": payload["name"], "description": payload["description"],
                                   "public": payload["public"], "owner": {"id": self.me}, "uris": []}
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
        dashboard.registerRoutes()
        self.dashboard = dashboard
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def testPreviewShowsSongsWithoutTouchingSpotify(self):
        html = self.client.get("/playlist-maker?kind=top-year&year=2020").get_data(as_text=True)
        self.assertIn("Top 50 of 2020", html)
        self.assertIn("Create playlist on Spotify", html)
        self.assertEqual(html.count('class="insight-entity"'), 3)
        self.assertEqual(self.spotifyFake.requests, [])

    def testPostFromAnotherSiteIsRejected(self):
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        noOrigin = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"})
        self.assertEqual(noOrigin.status_code, 403)
        self.assertEqual(self.spotifyFake.requests, [])

    def testBehindReverseProxyThePublicHostIsAccepted(self):
        response = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                    headers={"Origin": "https://host"})   #< SPOTIFY_REDIRECT_URI's host
        self.assertEqual(response.status_code, 302)
        forwarded = self.client.post("/playlist-maker", data={"kind": "top-year", "year": "2020"},
                                     headers={"Origin": "https://tracker.lan", "X-Forwarded-Host": "tracker.lan"})
        self.assertEqual(forwarded.status_code, 302)

    def testCreateThenUpdate(self):
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
