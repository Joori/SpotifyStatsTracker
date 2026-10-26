import unittest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Importers.StreamingHistoryImporter import Importer

musicoletCsv = (
    "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT\n"
    "/music/song.mp3,Song One,Artist One,Album One,Artist One,,Pop,2020,200000,1\n"
)

fakeTrack = {
    "id": "track123",
    "name": "Song One",
    "external_urls": {"spotify": "https://open.spotify.com/track/track123"},
    "duration_ms": 200000,
    "explicit": False,
    "disc_number": 1,
    "track_number": 1,
    "external_ids": {"isrc": "ABC123"},
    "album": {
        "id": "album123",
        "name": "Album One",
        "external_urls": {"spotify": "https://open.spotify.com/album/album123"},
        "images": [{"url": "https://example.com/img.jpg"}],
        "total_tracks": 10,
        "release_date": "2020-01-01",
        "artists": [{
            "name": "Artist One",
            "id": "artist123",
            "external_urls": {"spotify": "https://open.spotify.com/artist/artist123"},
        }],
    },
}


class FakeApi:
    """ Stands in for SpotifyApi: runs the call against a mocked spotipy client. """
    maxRetryWait = 0

    def __init__(self):
        self.client = MagicMock()
        self.calls = 0

    def call(self, fn, maxWait=0, onWait=None):
        self.calls += 1
        return fn(self.client)


class TestMusicoletImport(unittest.TestCase):
    def mockedImporter(self):
        importer = Importer(api=FakeApi())
        importer.sp = importer.api.client
        importer.sp.search.return_value = {"tracks": {"items": [fakeTrack]}}
        return importer

    def testImportHistoryDispatchesMusicoletWithoutError(self):
        importer = self.mockedImporter()
        parsedHistory, exportType = importer._convertToList(musicoletCsv)
        self.assertEqual(exportType, "musicoletPremium")

        progressCalls = []

        def progressCallback(status, current, total, message):
            progressCalls.append((status, current, total, message))

        result = importer.importHistory(parsedHistory, known=[], exportType=exportType, progressCallback=progressCallback)
        tracks = list(result)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["name"], "Song One")

    def testImportMusicoletCsvExportAcceptsProgressCallbackDirectly(self):
        importer = self.mockedImporter()
        gen = importer.importMusicoletCSVExport(musicoletCsv.splitlines()[1:], known=[], progressCallback=lambda *a: None)
        tracks = list(gen)
        self.assertEqual(len(tracks), 1)

    def testPrefetchProgressCallbackIsMonotonic(self):
        importer = self.mockedImporter()
        
        history = [
            ("Track A", "Artist A", "2023-01-01 00:00:00", 180000, "uriA"),
            ("Track B", "Artist B", "2023-01-01 00:03:00", 180000, "uriB"),
        ]
        
        importer.sp.track.side_effect = [
            {"id": "uriA", "name": "Track A", "external_urls": {"spotify": "http://a"}, "duration_ms": 180000, "album": {"images": []}},
            {"id": "uriB", "name": "Track B", "external_urls": {"spotify": "http://b"}, "duration_ms": 180000, "album": {"images": []}},
        ]
        
        progressCalls = []
        def progressCallback(status, current, total, message):
            progressCalls.append((status, current, total, message))
            
        def dummyDataFunction(item):
            return item
            
        tracks = list(importer._import(dummyDataFunction, history, known=[], progressCallback=progressCallback))
        
        prefetchCalls = [c for c in progressCalls if "Fetching track metadata" in c[3]]

        self.assertEqual([c[3] for c in prefetchCalls], [
            "Fetching track metadata (1/2)...",
            "Fetching track metadata (2/2)...",
        ])
        self.assertEqual(len(tracks), 2)

    def testKnownTracksSkipTheApi(self):
        importer = self.mockedImporter()
        known = [{"id": "uriA", "name": "Track A", "artists": [{"name": "Artist A"}]}]
        history = [("Track A", "Artist A", "2023-01-01 00:00:00", 180000, "uriA")]
        tracks = list(importer._import(lambda item: item, history, known=known))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(importer.api.calls, 0)

    def testRateLimitStopsImportInsteadOfStubbing(self):
        from Database.spotifyApi import RateLimitedError
        importer = self.mockedImporter()
        importer.api.call = MagicMock(side_effect=RateLimitedError(0))
        history = [("Track A", "Artist A", "2023-01-01 00:00:00", 180000, "uriA")]
        with self.assertRaises(RateLimitedError):
            list(importer._import(lambda item: item, history, known=[]))

    def testSearchUsesSingleResult(self):
        importer = self.mockedImporter()
        importer._searchForSong("Song One", "Artist One")
        importer.sp.search.assert_called_once_with("track:Song One artist:Artist One", type="track", limit=1)


if __name__ == "__main__":
    unittest.main()
