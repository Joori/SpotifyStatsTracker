import os
import sys
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from Database import profile as profileModule
from Database.profile import ProfileStore, pickAvatarUrl
from Database.spotifyApi import RateLimitedError


def pngBytes():
    buf = BytesIO()
    Image.new("RGB", (4, 4), "green").save(buf, format="PNG")
    return buf.getvalue()


class FakeApi:
    def __init__(self, me=None, error=None):
        self.me = me or {}
        self.error = error
        self.calls = 0
        self.connected = True

    def isConnected(self):
        return self.connected

    def call(self, fn, maxWait=0, onWait=None):
        self.calls += 1
        if self.error:
            raise self.error
        client = MagicMock()
        client._get.side_effect = lambda url, **kw: self.me if url == "me" else None
        return fn(client)


def fakeDownload(url, timeout=None):
    response = MagicMock(content=pngBytes())
    response.raise_for_status.return_value = None
    return response


ME = {"display_name": "Joori", "id": "u1", "images": [
    {"url": "https://i.scdn.co/big.jpg", "width": 300, "height": 300},
    {"url": "https://i.scdn.co/small.jpg", "width": 64, "height": 64},
]}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def testPicksSmallestUsableAvatar(self):
        self.assertEqual(pickAvatarUrl(ME["images"]), "https://i.scdn.co/small.jpg")
        self.assertEqual(pickAvatarUrl([{"url": "https://x/tiny.jpg", "width": 32}]), "https://x/tiny.jpg")
        self.assertIsNone(pickAvatarUrl([]))

    def testRefreshSavesNameAndAvatarLocally(self):
        store = ProfileStore(FakeApi(ME), self.folder)
        with patch.object(profileModule.requests, "get", side_effect=fakeDownload) as download:
            store.refresh()
            store.refresh()   #< same avatar URL: not downloaded again
        self.assertEqual(download.call_count, 1)
        self.assertEqual(store.get(), {"displayName": "Joori", "avatarFile": "avatar.png"})
        self.assertTrue((self.folder / "img" / "profile" / "avatar.png").is_file())
        self.assertTrue((self.folder / "profile.json").is_file())

    def testFreshCacheMakesNoCallsAndStaleCacheRefreshesInBackground(self):
        api = FakeApi(ME)
        store = ProfileStore(api, self.folder)
        release = threading.Event()
        realCall = api.call
        api.call = lambda fn, maxWait=0, onWait=None: (release.wait(5), realCall(fn, maxWait))[1]
        with patch.object(profileModule.requests, "get", side_effect=fakeDownload):
            self.assertEqual(store.get(), {})   #< nothing cached yet: page renders without waiting for Spotify
            release.set()
            store.refresher.thread.join(5)
            self.assertEqual(store.get()["displayName"], "Joori")
            self.assertEqual(api.calls, 1)      #< fresh now: no more calls

            store.cache.save(dict(store.cache.load(), fetchedAt=0))
            store.cache._data["fetchedAt"] = time.time() - 2 * 86400
            store.get()
            store.refresher.thread.join(5)
        self.assertEqual(api.calls, 2)

    def testRateLimitBacksOffInsteadOfRetryingEveryPage(self):
        api = FakeApi(error=RateLimitedError(time.time() + 3600))
        store = ProfileStore(api, self.folder)
        store.get()
        store.refresher.thread.join(5)
        store.get()
        store.get()
        self.assertEqual(api.calls, 1)
        self.assertEqual(store.get(), {})

    def testNoCallsWhenNotConnected(self):
        api = FakeApi(ME)
        api.connected = False
        store = ProfileStore(api, self.folder)
        self.assertEqual(store.get(), {})
        self.assertEqual(api.calls, 0)

    def testMissingAvatarFileIsNotLinked(self):
        store = ProfileStore(FakeApi(ME), self.folder)
        store.cache.save({"displayName": "Joori", "avatarUrl": "https://x", "avatarFile": "avatar.jpeg"})
        self.assertEqual(store.get(), {"displayName": "Joori", "avatarFile": None})


class SidebarProfileTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from app import SpotifyDashboardApp
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        dashboard = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dashboard.app = Flask("app", template_folder=str(root / "templates"), static_folder=str(root / "static"))
        dashboard.username = "test"
        dashboard.spotify = MagicMock(redirectUri="https://host/callback")
        dashboard.pendingOAuthStates = {}
        dashboard.database = MagicMock(listener=None)
        dashboard.database.readProgress.return_value = {}
        dashboard.profile = ProfileStore(FakeApi(ME), Path(self.tmp.name))
        dashboard.registerRoutes()
        self.dashboard = dashboard
        self.client = dashboard.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def testSidebarShowsNameAndAvatar(self):
        (Path(self.tmp.name) / "img" / "profile").mkdir(parents=True)
        (Path(self.tmp.name) / "img" / "profile" / "avatar.png").write_bytes(pngBytes())
        self.dashboard.profile.cache.save({"displayName": "Joori <b>", "avatarUrl": "u", "avatarFile": "avatar.png"})
        html = self.client.get("/import").get_data(as_text=True)
        self.assertIn('class="sidebar-profile"', html)
        self.assertIn("Joori &lt;b&gt;", html)
        self.assertIn('src="/img/test/profile/avatar.png"', html)

    def testSidebarFallsBackToInitialWithoutAvatar(self):
        self.dashboard.profile.cache.save({"displayName": "joori", "avatarUrl": None, "avatarFile": None})
        html = self.client.get("/import").get_data(as_text=True)
        self.assertIn("sidebar-avatar-initial", html)
        self.assertNotIn("/profile/", html)

    def testPageRendersWhenProfileIsBroken(self):
        self.dashboard.profile.get = MagicMock(side_effect=ValueError("bad file"))
        response = self.client.get("/import")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('class="sidebar-profile"', response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
