"""
"Spotify vs you": Spotify's own top artists/tracks (GET /me/top/{type}) compared with the
tracker's rankings over the same windows. Spotify's lists are cached in spotify_top.json for
a few hours and refreshed in the background when the Insights page asks for stale data.
"""
import datetime
import threading

try:
    from Database.spotifyCache import CachedJson, Refresher, BACKGROUND_MAX_WAIT
except ModuleNotFoundError:
    from spotifyCache import CachedJson, Refresher, BACKGROUND_MAX_WAIT

TOP_MAX_AGE = 6 * 3600
TIME_RANGES = (
    # Spotify's time_range, days the tracker compares against, label
    ("short_term", 28, "Last 4 weeks"),
    ("medium_term", 182, "Last 6 months"),
    ("long_term", 365, "Last year"),
)
SPOTIFY_LIMIT = 50
HIGHLIGHT_TOP = 20     #< "ranks high" = in the top 20
RARE_PLAYS = 2         #< "rarely played" = this many plays or fewer in the window ...
RARE_RANK = 50         #< ... or ranked below the tracker's top 50


def _largestFirstImage(images, target=300):
    images = sorted([i for i in images or [] if isinstance(i, dict) and i.get("url")], key=lambda i: i.get("width") or 0)
    for image in images:
        if (image.get("width") or 0) >= target:
            return image["url"]
    return images[-1]["url"] if images else None


def slimTrack(item):
    album = item.get("album") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "artists": [{"id": a.get("id"), "name": a.get("name")} for a in item.get("artists") or []],
        "isrc": (item.get("external_ids") or {}).get("isrc"),
        "imageUrl": _largestFirstImage(album.get("images")),
        "url": (item.get("external_urls") or {}).get("spotify"),
    }


def slimArtist(item):
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "imageUrl": _largestFirstImage(item.get("images")),
        "url": (item.get("external_urls") or {}).get("spotify"),
    }


def _songKey(name, artists):
    first = (artists or [{}])[0] or {}
    return f"{(name or '').strip().lower()}|{(first.get('name') or '').strip().lower()}"


class TrackIndex:
    """ Finds the tracker's track IDs for a Spotify track: same ID, else same ISRC, else same name + first artist. """
    def __init__(self, tracks):
        self.ids = set(tracks)
        self.byIsrc, self.byKey = {}, {}
        for tid, track in tracks.items():
            if track.get("isrc"):
                self.byIsrc.setdefault(track["isrc"], []).append(tid)
            self.byKey.setdefault(_songKey(track.get("name"), track.get("artists")), []).append(tid)

    def match(self, spotifyTrack):
        tid = spotifyTrack.get("id")
        found = [tid] if tid in self.ids else []
        for other in self.byIsrc.get(spotifyTrack.get("isrc"), []) + self.byKey.get(_songKey(spotifyTrack.get("name"), spotifyTrack.get("artists")), []):
            if other not in found:
                found.append(other)
        return found


def localRankings(entries, tracks):
    """ Plays per track and per artist for already-filtered entries, with 1-based ranks. """
    trackPlays, artistPlays, artistNames = {}, {}, {}
    for entry in entries:
        tid = entry.get("id")
        if not tid:
            continue
        trackPlays[tid] = trackPlays.get(tid, 0) + 1
        for artist in (tracks.get(tid) or {}).get("artists") or []:
            aid = artist.get("id") if isinstance(artist, dict) else None
            if aid:
                artistPlays[aid] = artistPlays.get(aid, 0) + 1
                artistNames.setdefault(aid, artist.get("name"))

    def ranked(plays):
        order = sorted(plays, key=lambda k: (-plays[k], k))
        return {k: i + 1 for i, k in enumerate(order)}, order

    trackRank, trackOrder = ranked(trackPlays)
    artistRank, artistOrder = ranked(artistPlays)
    return {
        "trackPlays": trackPlays, "trackRank": trackRank, "trackOrder": trackOrder,
        "artistPlays": artistPlays, "artistRank": artistRank, "artistOrder": artistOrder, "artistNames": artistNames,
    }


def _artistsText(artists):
    return ", ".join(a.get("name", "") for a in artists or [] if isinstance(a, dict) and a.get("name"))


def compareTracks(spotifyTracks, local, tracks, index):
    spotifyLocalIds = set()
    rows = []
    for rank, item in enumerate(spotifyTracks, start=1):
        ids = index.match(item)
        spotifyLocalIds.update(ids)
        plays = sum(local["trackPlays"].get(i, 0) for i in ids)
        localRank = min((local["trackRank"][i] for i in ids if i in local["trackRank"]), default=None)
        localId = next((i for i in ids if i in local["trackRank"]), ids[0] if ids else None)
        rows.append({
            "name": item.get("name"), "subtitle": _artistsText(item.get("artists")),
            "spotifyRank": rank, "localRank": localRank, "plays": plays,
            "imageId": (tracks.get(localId) or {}).get("imageId") if localId else None,
            "imageUrl": item.get("imageUrl"), "localId": localId, "url": item.get("url"),
        })
    spotifyHigh = [r for r in rows if r["spotifyRank"] <= HIGHLIGHT_TOP
                   and (r["plays"] <= RARE_PLAYS or r["localRank"] is None or r["localRank"] > RARE_RANK)]

    youHigh = []
    for tid in local["trackOrder"][:HIGHLIGHT_TOP]:
        if tid in spotifyLocalIds:
            continue
        track = tracks.get(tid) or {}
        youHigh.append({
            "name": track.get("name", "Unknown"), "subtitle": _artistsText(track.get("artists")),
            "spotifyRank": None, "localRank": local["trackRank"][tid], "plays": local["trackPlays"][tid],
            "imageId": track.get("imageId"), "imageUrl": None, "localId": tid, "url": track.get("url"),
        })
    agree = sum(1 for r in rows[:HIGHLIGHT_TOP] if r["localRank"] is not None and r["localRank"] <= RARE_RANK)
    return {"spotifyHigh": spotifyHigh, "youHigh": youHigh, "agree": agree,
            "compared": min(HIGHLIGHT_TOP, len(rows)), "spotifyCount": len(rows)}


def compareArtists(spotifyArtists, local):
    rows = []
    for rank, item in enumerate(spotifyArtists, start=1):
        aid = item.get("id")
        rows.append({
            "name": item.get("name"), "subtitle": None, "spotifyRank": rank,
            "localRank": local["artistRank"].get(aid), "plays": local["artistPlays"].get(aid, 0),
            "imageUrl": item.get("imageUrl"), "imageId": None, "localId": aid, "url": item.get("url"),
        })
    spotifyIds = {item.get("id") for item in spotifyArtists}
    spotifyHigh = [r for r in rows if r["spotifyRank"] <= HIGHLIGHT_TOP
                   and (r["plays"] <= RARE_PLAYS or r["localRank"] is None or r["localRank"] > RARE_RANK)]
    youHigh = [{
        "name": local["artistNames"].get(aid) or "Unknown", "subtitle": None, "spotifyRank": None,
        "localRank": local["artistRank"][aid], "plays": local["artistPlays"][aid],
        "imageUrl": None, "imageId": None, "localId": aid, "url": None,
    } for aid in local["artistOrder"][:HIGHLIGHT_TOP] if aid not in spotifyIds] if spotifyArtists else []
    agree = sum(1 for r in rows[:HIGHLIGHT_TOP] if r["localRank"] is not None and r["localRank"] <= RARE_RANK)
    return {"spotifyHigh": spotifyHigh, "youHigh": youHigh, "agree": agree,
            "compared": min(HIGHLIGHT_TOP, len(rows)), "spotifyCount": len(rows)}


class SpotifyTopStore:
    def __init__(self, api, userFolder, maxAge=TOP_MAX_AGE):
        self.api = api
        self.cache = CachedJson(userFolder / "spotify_top.json")
        self.maxAge = maxAge
        self.refresher = Refresher("Spotify top lists", self.refresh, api)
        self._resultLock = threading.Lock()
        self._result = (None, None)   #< (cache key, comparison)

    def refresh(self):
        data = {"tracks": {}, "artists": {}}
        for timeRange, _, _ in TIME_RANGES:
            for kind, slim in (("tracks", slimTrack), ("artists", slimArtist)):
                page = self.api.call(lambda c, k=kind, t=timeRange: c._get(f"me/top/{k}", time_range=t, limit=SPOTIFY_LIMIT),
                                     maxWait=BACKGROUND_MAX_WAIT) or {}
                data[kind][timeRange] = [slim(i) for i in page.get("items") or [] if i and i.get("id")]
        self.cache.save(data)

    def comparison(self, database, now=None):
        """ Comparison for the Insights page, or a status dict while Spotify's lists aren't cached yet. """
        if self.cache.isStale(self.maxAge):
            self.refresher.refreshInBackground()
        cached = self.cache.load()
        if not cached.get("fetchedAt"):
            return {"ready": False, "refreshing": self.refresher.running,
                    "error": self.refresher.lastError}
        key = (cached.get("fetchedAt"), getattr(database, "dataVersion", None))
        with self._resultLock:
            if self._result[0] == key:
                return self._result[1]
        result = self._compare(cached, database, now)
        with self._resultLock:
            self._result = (key, result)
        return result

    def _compare(self, cached, database, now=None):
        now = now or datetime.datetime.now().astimezone()
        tracks = database._loadTracks() or {}
        entries = database._loadEntries()
        index = TrackIndex(tracks)
        windows = []
        for timeRange, days, label in TIME_RANGES:
            inWindow = database.filterByInterval(entries, now - datetime.timedelta(days=days), now + datetime.timedelta(days=1))
            local = localRankings(inWindow, tracks)
            windows.append({
                "key": timeRange, "label": label, "days": days, "localPlays": len(inWindow),
                "tracks": compareTracks((cached.get("tracks") or {}).get(timeRange) or [], local, tracks, index),
                "artists": compareArtists((cached.get("artists") or {}).get(timeRange) or [], local),
            })
        return {"ready": True, "fetchedAt": cached.get("fetchedAt"), "windows": windows}
