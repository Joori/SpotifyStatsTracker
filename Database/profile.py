"""
Spotify profile (display name + avatar) for the sidebar. GET /me, cached in profile.json and
refreshed at most daily, when a page is opened. The avatar is downloaded to img/profile/.
"""
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

try:
    from Database.spotifyCache import CachedJson, Refresher, BACKGROUND_MAX_WAIT
except ModuleNotFoundError:
    from spotifyCache import CachedJson, Refresher, BACKGROUND_MAX_WAIT

PROFILE_MAX_AGE = 24 * 3600
AVATAR_MIN_SIZE = 64


def pickAvatarUrl(images):
    """ Smallest image that's still at least AVATAR_MIN_SIZE wide, else the largest. """
    images = [i for i in images or [] if isinstance(i, dict) and i.get("url")]
    if not images:
        return None
    sized = sorted(images, key=lambda i: i.get("width") or 0)
    for image in sized:
        if (image.get("width") or 0) >= AVATAR_MIN_SIZE:
            return image["url"]
    return sized[-1]["url"]


class ProfileStore:
    def __init__(self, api, userFolder, maxAge=PROFILE_MAX_AGE):
        self.api = api
        self.userFolder = Path(userFolder)
        self.imgDir = self.userFolder / "img" / "profile"
        self.cache = CachedJson(self.userFolder / "profile.json")
        self.maxAge = maxAge
        self.refresher = Refresher("Profile", self.refresh, api)

    def get(self):
        """ Cached profile for templates: {"displayName", "avatarFile"} or {}. Kicks off a refresh when stale. """
        if self.cache.isStale(self.maxAge):
            self.refresher.refreshInBackground()
        data = self.cache.load()
        avatar = data.get("avatarFile")
        if avatar and not (self.imgDir / avatar).is_file():
            avatar = None
        return {"displayName": data.get("displayName"), "avatarFile": avatar} if data.get("displayName") else {}

    def refresh(self):
        me = self.api.call(lambda c: c._get("me"), maxWait=BACKGROUND_MAX_WAIT) or {}
        old = self.cache.load()
        avatarFile = old.get("avatarFile")
        avatarUrl = pickAvatarUrl(me.get("images"))
        if avatarUrl and avatarUrl != old.get("avatarUrl"):
            avatarFile = self._downloadAvatar(avatarUrl) or avatarFile
        elif not avatarUrl:
            avatarFile = None
        self.cache.save({
            "displayName": me.get("display_name") or me.get("id") or "",
            "avatarUrl": avatarUrl,
            "avatarFile": avatarFile,
        })

    def _downloadAvatar(self, url):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            ext = (img.format or "jpeg").lower()
            self.imgDir.mkdir(parents=True, exist_ok=True)
            filename = f"avatar.{ext}"
            img.save(self.imgDir / filename)
            for other in self.imgDir.glob("avatar.*"):
                if other.name != filename:
                    other.unlink(missing_ok=True)
            return filename
        except Exception as e:
            print(f"Couldn't save the Spotify avatar: {e}")
            return None
