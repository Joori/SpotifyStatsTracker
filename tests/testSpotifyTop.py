import datetime
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Database.spotifyTop import (SpotifyTopStore, TrackIndex, compareArtists, compareTracks,
                                 localRankings, slimTrack, TIME_RANGES)

NOW = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone.utc)


def track(tid, name, artist="A", artistId="a1", isrc=None, imageId=None):
    return {"id": tid, "name": name, "isrc": isrc, "imageId": imageId,
            "artists": [{"id": artistId, "name": artist}], "url": f"https://open.spotify.com/track/{tid}"}


def spotifyTrack(tid, name, artist="A", artistId="a1", isrc=None):
    return {"id": tid, "name": name, "isrc": isrc, "artists": [{"id": artistId, "name": artist}],
            "imageUrl": "https://i.scdn.co/x", "url": f"https://open.spotify.com/track/{tid}"}


def plays(tid, count):
    return [{"id": tid, "playedAt": 0, "timePlayed": 1000}] * count


TRACKS = {
    "loved": track("loved", "Loved", imageId="img1"),
    "relinked": track("relinked-old", "Relinked", isrc="ISRC1"),
    "renamed": track("renamed-local", "Same Name", artist="B", artistId="a2"),
    "ignored": track("ignored", "Ignored"),
}
TRACKS = {t["id"]: t for t in TRACKS.values()}


class MatchingTests(unittest.TestCase):
    def testMatchesByIdThenIsrcThenNameAndArtist(self):
        index = TrackIndex(TRACKS)
        self.assertEqual(index.match(spotifyTrack("loved", "Loved")), ["loved"])
        self.assertEqual(index.match(spotifyTrack("relinked-new", "Other title", isrc="ISRC1")), ["relinked-old"])
        self.assertEqual(index.match(spotifyTrack("renamed-spotify", "same name ", artist="b")), ["renamed-local"])
        self.assertEqual(index.match(spotifyTrack("new", "Never seen")), [])


class CompareTests(unittest.TestCase):
    def setUp(self):
        entries = plays("loved", 30) + plays("relinked-old", 10) + plays("ignored", 1)
        self.local = localRankings(entries, TRACKS)
        self.index = TrackIndex(TRACKS)

    def testHighlightsBothDirections(self):
        spotify = [spotifyTrack("relinked-new", "Relinked", isrc="ISRC1"),   #< played 10x under its old ID
                   spotifyTrack("ignored", "Ignored"),                       #< 1 play: rarely
                   spotifyTrack("new", "Never seen")]                        #< 0 plays
        result = compareTracks(spotify, self.local, TRACKS, self.index)

        self.assertEqual([r["name"] for r in result["spotifyHigh"]], ["Ignored", "Never seen"])
        self.assertEqual(result["spotifyHigh"][0]["plays"], 1)
        self.assertIsNone(result["spotifyHigh"][1]["localId"])
        self.assertEqual([r["name"] for r in result["youHigh"]], ["Loved"])   #< relinked counts as listed
        self.assertEqual(result["youHigh"][0]["imageId"], "img1")
        self.assertEqual((result["agree"], result["compared"], result["spotifyCount"]), (2, 3, 3))

    def testArtistsComparedById(self):
        result = compareArtists([{"id": "a9", "name": "Spotify Pick", "imageUrl": None, "url": None}], self.local)
        self.assertEqual([r["name"] for r in result["spotifyHigh"]], ["Spotify Pick"])
        self.assertEqual([r["name"] for r in result["youHigh"]], ["A"])

    def testNoSpotifyArtistsMeansNoClaimsAboutThem(self):
        result = compareArtists([], self.local)
        self.assertEqual((result["spotifyHigh"], result["youHigh"], result["spotifyCount"]), ([], [], 0))


class FakeApi:
    def __init__(self):
        self.calls = []

    def isConnected(self):
        return True

    def call(self, fn, maxWait=0, onWait=None):
        client = MagicMock()

        def get(url, **kw):
            self.calls.append((url, kw["time_range"], kw["limit"], maxWait))
            if url == "me/top/artists" and kw["time_range"] == "short_term":
                return {"items": []}   #< what Spotify returned in the probe
            return {"items": [{
                "id": "t1", "name": "Song", "popularity": 50, "external_ids": {"isrc": "I1"},
                "artists": [{"id": "a1", "name": "A", "href": "x"}],
                "album": {"images": [{"url": "https://i.scdn.co/640", "width": 640}, {"url": "https://i.scdn.co/300", "width": 300}]},
                "external_urls": {"spotify": "https://open.spotify.com/track/t1"},
            }]}
        client._get.side_effect = get
        return fn(client)


class FakeDatabase:
    def __init__(self, tracks, entries):
        self.tracks, self.entries, self.dataVersion = tracks, entries, "v1"

    def _loadTracks(self):
        return self.tracks

    def _loadEntries(self):
        return self.entries

    def filterByInterval(self, entries, start, end):
        return [e for e in entries if start.timestamp() <= e["playedAt"] <= end.timestamp()]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeApi()
        self.store = SpotifyTopStore(self.api, Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def testRefreshFetchesSixListsAndStoresOnlyWhatItNeeds(self):
        self.store.refresh()
        self.assertEqual(len(self.api.calls), 6)
        self.assertEqual({c[1] for c in self.api.calls}, {"short_term", "medium_term", "long_term"})
        self.assertTrue(all(c[2] == 50 and c[3] > 0 for c in self.api.calls))   #< background may wait out short 429s
        cached = self.store.cache.load()
        self.assertEqual(cached["artists"]["short_term"], [])
        self.assertEqual(cached["tracks"]["long_term"][0], {
            "id": "t1", "name": "Song", "artists": [{"id": "a1", "name": "A"}], "isrc": "I1",
            "imageUrl": "https://i.scdn.co/300", "url": "https://open.spotify.com/track/t1"})

    def testNotReadyUntilFetchedThenComparesAndCaches(self):
        db = FakeDatabase({"t1": track("t1", "Song", isrc="I1")},
                          [{"id": "t1", "playedAt": NOW.timestamp() - 86400, "timePlayed": 1}])
        self.store.refresher.refreshInBackground = MagicMock(return_value=True)
        first = self.store.comparison(db, now=NOW)
        self.assertFalse(first["ready"])
        self.store.refresher.refreshInBackground.assert_called_once()
        self.store.refresher.refreshNow()

        result = self.store.comparison(db, now=NOW)
        self.assertTrue(result["ready"])
        self.assertEqual([w["key"] for w in result["windows"]], [t[0] for t in TIME_RANGES])
        self.assertEqual(result["windows"][0]["tracks"]["agree"], 1)
        self.assertEqual(result["windows"][0]["artists"]["spotifyCount"], 0)

        self.store._compare = MagicMock(side_effect=AssertionError("should be cached"))
        self.assertIs(self.store.comparison(db, now=NOW), result)
        db.dataVersion = "v2"   #< new plays: recompute
        self.store._compare = MagicMock(return_value={"ready": True, "windows": []})
        self.store.comparison(db, now=NOW)
        self.store._compare.assert_called_once()


if __name__ == "__main__":
    unittest.main()
