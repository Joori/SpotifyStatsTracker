import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Database.likedSongs import LikedStore, compareLiked, HEAVY_PLAYS
from Database.spotifyApi import RateLimitedError


def savedItem(tid, name, addedAt, isrc=None, local=False):
    return {"added_at": addedAt, "track": {
        "id": tid, "name": name, "is_local": local, "external_ids": {"isrc": isrc},
        "artists": [{"id": "a1", "name": "A"}], "album": {"images": []},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"}}}


class PagedApi:
    """ Serves `pages` in order; raises `failOn` (page index) with a rate limit if set. """
    def __init__(self, pages, failOn=None):
        self.pages, self.failOn, self.requests = pages, failOn, []

    def isConnected(self):
        return True

    def call(self, fn, maxWait=0, onWait=None):
        client = MagicMock()

        def get(url, limit, offset):
            self.requests.append((url, limit, offset, maxWait))
            page = offset // limit
            if page == self.failOn:
                raise RateLimitedError(time.time() + 3600)
            items = self.pages[page]
            return {"items": items, "total": sum(len(p) for p in self.pages),
                    "next": "more" if page + 1 < len(self.pages) else None}
        client._get.side_effect = get
        return fn(client)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def pages(self):
        first = [savedItem(f"t{i}", f"Song {i}", "2026-01-01T00:00:00Z") for i in range(50)]
        second = [savedItem("t50", "Song 50", "2025-01-01T00:00:00Z"),
                  savedItem("local1", "Local file", "2025-01-01T00:00:00Z", local=True)]
        return [first, second]

    def testPaginatesThroughSharedClientAndSkipsLocalFiles(self):
        api = PagedApi(self.pages())
        store = LikedStore(api, self.folder, pagePause=0)
        store.refresh()
        self.assertEqual([(r[1], r[2]) for r in api.requests], [(50, 0), (50, 50)])
        self.assertTrue(all(r[0] == "me/tracks" and r[3] > 0 for r in api.requests))
        cached = store.cache.load()
        self.assertEqual(len(cached["tracks"]), 51)
        self.assertEqual(cached["tracks"][50]["addedAt"], "2025-01-01T00:00:00Z")

    def testRateLimitHalfwayKeepsThePreviousList(self):
        store = LikedStore(PagedApi(self.pages()), self.folder, pagePause=0)
        store.refresh()
        before = store.cache.load()

        store = LikedStore(PagedApi(self.pages(), failOn=1), self.folder, pagePause=0)
        self.assertFalse(store.refresher.refreshNow())
        self.assertEqual(store.cache.load(), before)
        self.assertIn("rate limit", store.refresher.lastError)


class CompareTests(unittest.TestCase):
    TRACKS = {
        "loved": {"id": "loved", "name": "Loved", "isrc": "L1", "artists": [{"id": "a1", "name": "A"}], "imageId": "i1"},
        "heavy": {"id": "heavy", "name": "Heavy", "isrc": "H1", "artists": [{"id": "a1", "name": "A"}]},
        "light": {"id": "light", "name": "Light", "isrc": "X1", "artists": [{"id": "a1", "name": "A"}]},
    }

    def liked(self, tid, name, addedAt, isrc=None):
        return {"id": tid, "name": name, "isrc": isrc, "artists": [{"id": "a1", "name": "A"}],
                "imageUrl": "https://i.scdn.co/x", "url": "u", "addedAt": addedAt}

    def testBothLists(self):
        entries = ([{"id": "loved"}] * 40 + [{"id": "heavy"}] * HEAVY_PLAYS
                   + [{"id": "light"}] * (HEAVY_PLAYS - 1))
        liked = [
            self.liked("loved-relinked", "Loved", "2024-01-01", isrc="L1"),   #< liked under another ID
            self.liked("old-never", "Old never", "2020-01-01"),
            self.liked("new-never", "New never", "2026-01-01"),
        ]
        result = compareLiked(liked, self.TRACKS, entries)
        self.assertEqual([r["name"] for r in result["likedRarely"]], ["New never", "Old never"])
        self.assertEqual((result["likedRarelyCount"], result["likedNeverCount"]), (2, 2))
        self.assertIsNone(result["likedRarely"][0]["localId"])
        self.assertEqual([r["name"] for r in result["playedNotLiked"]], ["Heavy"])   #< "light" is under the threshold

    def testRarelyPlayedSortsByPlaysThenNewestLiked(self):
        entries = [{"id": "light"}] * 2
        liked = [self.liked("light", "Light", "2025-06-01", isrc="X1"), self.liked("n", "Never", "2019-01-01")]
        result = compareLiked(liked, self.TRACKS, entries)
        self.assertEqual([(r["name"], r["plays"]) for r in result["likedRarely"]], [("Never", 0), ("Light", 2)])
        self.assertEqual(result["likedRarely"][1]["localId"], "light")


if __name__ == "__main__":
    unittest.main()
