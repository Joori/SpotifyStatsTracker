"""
Liked vs played. Liked songs (GET /me/tracks, 50 per page) are synced to liked.json at most
daily, in the background when the Insights page asks. A sync only replaces the stored list once
every page has arrived, so a rate limit halfway through keeps the previous list.
"""
import threading
import time

try:
    from Database.spotifyCache import CachedJson, Refresher, BACKGROUND_MAX_WAIT
    from Database.spotifyTop import TrackIndex, slimTrack
except ModuleNotFoundError:
    from spotifyCache import CachedJson, Refresher, BACKGROUND_MAX_WAIT
    from spotifyTop import TrackIndex, slimTrack

LIKED_MAX_AGE = 24 * 3600
PAGE_SIZE = 50
MAX_PAGES = 400            #< 20,000 songs; a safety stop, not a real limit for most libraries
PAGE_PAUSE = 0.2           #< seconds between pages, to be gentle on the shared rate limit
RARELY_PLAYED = 2          #< "liked but rarely played": this many plays or fewer, all time
HEAVY_PLAYS = 10           #< "heavily played": at least this many plays, all time
LIST_LIMIT = 100


class LikedStore:
    def __init__(self, api, userFolder, maxAge=LIKED_MAX_AGE, pagePause=PAGE_PAUSE):
        self.api = api
        self.cache = CachedJson(userFolder / "liked.json")
        self.maxAge = maxAge
        self.pagePause = pagePause
        self.refresher = Refresher("Liked songs", self.refresh, api)
        self._resultLock = threading.Lock()
        self._result = (None, None)

    def refresh(self):
        liked, offset, total = [], 0, None
        for _ in range(MAX_PAGES):
            page = self.api.call(lambda c, o=offset: c._get("me/tracks", limit=PAGE_SIZE, offset=o),
                                 maxWait=BACKGROUND_MAX_WAIT) or {}
            items = page.get("items") or []
            total = page.get("total", total)
            for item in items:
                track = (item or {}).get("track") or {}
                if track.get("id") and not track.get("is_local"):
                    liked.append(dict(slimTrack(track), addedAt=item.get("added_at")))
            offset += len(items)
            if not items or not page.get("next"):
                break
            time.sleep(self.pagePause)
        self.cache.save({"tracks": liked, "total": total if total is not None else len(liked)})

    def comparison(self, database):
        if self.cache.isStale(self.maxAge):
            self.refresher.refreshInBackground()
        cached = self.cache.load()
        if not cached.get("fetchedAt"):
            return {"ready": False, "refreshing": self.refresher.running, "error": self.refresher.lastError}
        key = (cached.get("fetchedAt"), getattr(database, "dataVersion", None))
        with self._resultLock:
            if self._result[0] == key:
                return self._result[1]
        result = compareLiked(cached.get("tracks") or [], database._loadTracks() or {}, database._loadEntries())
        result.update(ready=True, fetchedAt=cached.get("fetchedAt"), likedTotal=len(cached.get("tracks") or []))
        with self._resultLock:
            self._result = (key, result)
        return result


def compareLiked(liked, tracks, entries):
    plays = {}
    for entry in entries:
        tid = entry.get("id")
        if tid:
            plays[tid] = plays.get(tid, 0) + 1
    index = TrackIndex(tracks)

    likedLocalIds = set()
    rarely = []
    for song in liked:
        ids = index.match(song)
        likedLocalIds.update(ids)
        count = sum(plays.get(i, 0) for i in ids)
        if count <= RARELY_PLAYED:
            localId = next((i for i in ids if plays.get(i)), ids[0] if ids else None)
            rarely.append({
                "name": song.get("name"),
                "subtitle": ", ".join(a.get("name", "") for a in song.get("artists") or [] if a.get("name")),
                "plays": count, "addedAt": song.get("addedAt"), "localId": localId if count else None,
                "imageId": (tracks.get(localId) or {}).get("imageId") if localId else None,
                "imageUrl": song.get("imageUrl"), "url": song.get("url"),
            })
    # Never played first, then the ones you liked most recently
    rarely.sort(key=lambda r: r["addedAt"] or "", reverse=True)
    rarely.sort(key=lambda r: r["plays"])

    notLiked = []
    for tid in sorted(plays, key=lambda t: (-plays[t], t)):
        if plays[tid] < HEAVY_PLAYS or len(notLiked) >= LIST_LIMIT:
            break
        if tid in likedLocalIds:
            continue
        track = tracks.get(tid) or {}
        notLiked.append({
            "name": track.get("name", "Unknown"),
            "subtitle": ", ".join(a.get("name", "") for a in track.get("artists") or [] if isinstance(a, dict) and a.get("name")),
            "plays": plays[tid], "addedAt": None, "localId": tid,
            "imageId": track.get("imageId"), "imageUrl": None, "url": track.get("url"),
        })
    return {
        "likedRarely": rarely[:LIST_LIMIT],
        "likedRarelyCount": len(rarely),
        "likedNeverCount": sum(1 for r in rarely if r["plays"] == 0),
        "playedNotLiked": notLiked,
    }
