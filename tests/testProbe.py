import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spotipy.exceptions import SpotifyException

from Database.spotifyApi import SpotifyApi, SCOPES
from tools import probeSpotifyApi


class FakeClient:
    def __init__(self):
        self.posts = []

    def _get(self, url, **kwargs):
        if url == "me":
            return {"display_name": "Secret Person", "id": "user123", "images": [{"url": "https://img/secret.jpg"}]}
        if url == "me/player/currently-playing":
            return None   #< 204
        if url.startswith("me/top/") or url in ("me/tracks", "me/player/recently-played"):
            track = {"uri": f"spotify:track:t{len(url)}", "name": "Hidden Song"}
            items = [{"track": track}] if url in ("me/tracks", "me/player/recently-played") else [track, {"uri": "spotify:track:other"}]
            return {"items": items, "total": 2}
        if url == "me/playlists":
            raise SpotifyException(403, -1, "playlists: Forbidden")
        return {"id": "pl1", "items": []}

    def _post(self, url, payload=None, **kwargs):
        self.posts.append((url, payload))
        return {"id": "pl1", "snapshot_id": "snap"}

    def _put(self, url, payload=None, **kwargs):
        return None


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "token"
        self.lines = []
        self.client = FakeClient()

    def tearDown(self):
        self.tmp.cleanup()

    def _saveToken(self, scope):
        api = SpotifyApi(self.cache, "id", "secret", "https://host/callback")
        api.cacheHandler.save_token_to_cache({"access_token": "TOPSECRET", "refresh_token": "REFRESHSECRET",
                                              "expires_at": time.time() + 3600, "scope": scope})

    def _run(self, *argv):
        fakeClient = self.client

        class ProbeApi(SpotifyApi):
            def __init__(api, cacheFile):
                super().__init__(cacheFile, "id", "secret", "https://host/callback")
                api.client = fakeClient   #< real call()/429 handling, fake HTTP

        probeSpotifyApi.SpotifyApi = ProbeApi
        try:
            return probeSpotifyApi.main(["--token-cache", str(self.cache), *argv], out=self.lines.append)
        finally:
            probeSpotifyApi.SpotifyApi = SpotifyApi

    def testReportsStatusAndFieldNamesButNoValuesOrTokens(self):
        self._saveToken(SCOPES)
        code = self._run("--write")
        text = "\n".join(self.lines)

        self.assertEqual(code, 1)   #< the 403 counts as a failure
        self.assertIn("OK   200  GET /me", text)
        self.assertIn("display_name", text)
        self.assertIn("OK   204  GET /me/player/currently-playing", text)
        self.assertIn("FAIL 403  GET /me/playlists", text)
        for secret in ("Secret Person", "user123", "secret.jpg", "Hidden Song", "TOPSECRET", "REFRESHSECRET"):
            self.assertNotIn(secret, text)
        created = [p for url, p in self.client.posts if url == "me/playlists"]
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0]["public"])

    def testReadOnlyByDefault(self):
        self._saveToken(SCOPES)
        self._run()
        self.assertEqual(self.client.posts, [])

    def testStopsBeforeCallingWhenScopesAreMissing(self):
        self._saveToken("user-read-recently-played playlist-read-private")
        self.client._get = MagicMock()
        self.assertEqual(self._run(), 2)
        self.client._get.assert_not_called()
        self.assertIn("lacks scopes", self.lines[0])


if __name__ == "__main__":
    unittest.main()
