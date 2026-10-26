"""
Playlist generator: private playlists made from your history.

Generators pick track IDs from the tracker's own data; nothing is sent to Spotify until you
press Create/Update. Playlists are created with POST /me/playlists and filled with
PUT/POST /playlists/{id}/items (the endpoints that replaced the deprecated ones in 2026).
The playlist each generator made is remembered in generated_playlists.json, so running it
again updates that playlist instead of making a duplicate.
"""
import datetime
import re
import threading

from spotipy.exceptions import SpotifyException

try:
    from Database.spotifyCache import CachedJson
    from Database.utils import convertToDatetime
    from Database import analytics
except ModuleNotFoundError:
    from spotifyCache import CachedJson
    from utils import convertToDatetime
    import analytics

PLAYLIST_SIZE = 50
ITEMS_PER_REQUEST = 100
MARKER = "Made by SpotifyStatsTracker."
FORGOTTEN_MIN_PLAYS = 10       #< "a favourite" in the past period
FORGOTTEN_MAX_RECENT = 2       #< "forgotten" = this many plays or fewer in the last 12 months
RECENT_DAYS = 365
SPOTIFY_ID = re.compile(r"[0-9A-Za-z]{22}")


class GeneratorError(Exception):
    """ Bad generator parameters, shown to the user as-is. """


def _plays(entries, start=None, end=None):
    counts = {}
    for entry in entries:
        tid, playedAt = entry.get("id"), entry.get("playedAt")
        if not tid or playedAt is None:
            continue
        if start is not None and playedAt < start:
            continue
        if end is not None and playedAt >= end:
            continue
        counts[tid] = counts.get(tid, 0) + 1
    return counts


def _ranked(counts, minPlays=1):
    return [t for t in sorted(counts, key=lambda t: (-counts[t], t)) if counts[t] >= minPlays]


def _yearBounds(year):
    tz = datetime.datetime.now().astimezone().tzinfo
    start = datetime.datetime(year, 1, 1, tzinfo=tz).timestamp()
    end = datetime.datetime(year + 1, 1, 1, tzinfo=tz).timestamp()
    return start, end


def generate(kind, params, entries, tracks, now=None):
    """
    Returns {"key", "name", "description", "tracks": [{"id", "detail"}]} for one generator.
    kind: "top-year" (params: year), "forgotten" (params: year, optional), "peak-obsessions".
    """
    now = now or datetime.datetime.now().astimezone()
    usable = lambda tid: tid in tracks and SPOTIFY_ID.fullmatch(tid or "")

    if kind == "top-year":
        year = _intParam(params, "year")
        if year is None:
            raise GeneratorError("Pick a year.")
        start, end = _yearBounds(year)
        counts = _plays(entries, start, end)
        ids = [t for t in _ranked(counts) if usable(t)][:PLAYLIST_SIZE]
        return {
            "key": f"top-year-{year}",
            "name": f"Top 50 of {year}",
            "description": f"Your most played songs of {year}. {MARKER}",
            "tracks": [{"id": t, "detail": f"{counts[t]} plays in {year}"} for t in ids],
        }

    if kind == "forgotten":
        recentStart = (now - datetime.timedelta(days=RECENT_DAYS)).timestamp()
        year = _intParam(params, "year")
        if year is not None:
            start, end = _yearBounds(year)
            if end > recentStart:
                raise GeneratorError("Pick a year that ended more than 12 months ago.")
            period = str(year)
        else:
            start, end = None, recentStart
            period = "before the last 12 months"
        past = _plays(entries, start, end)
        recent = _plays(entries, recentStart)
        ids = [t for t in _ranked(past, FORGOTTEN_MIN_PLAYS)
               if recent.get(t, 0) <= FORGOTTEN_MAX_RECENT and usable(t)][:PLAYLIST_SIZE]

        def detail(t):
            r = recent.get(t, 0)
            since = "none" if r == 0 else str(r)
            return f"{past[t]} plays {'in ' + period if year is not None else period} · {since} in the last 12 months"
        suffix = f" ({year})" if year is not None else ""
        return {
            "key": f"forgotten-{year}" if year is not None else "forgotten",
            "name": f"Forgotten favourites{suffix}",
            "description": (f"Songs you played a lot {'in ' + period if year is not None else period} "
                            f"but {FORGOTTEN_MAX_RECENT} times or fewer in the last 12 months. {MARKER}"),
            "tracks": [{"id": t, "detail": detail(t)} for t in ids],
        }

    if kind == "peak-obsessions":
        peaks = analytics.peakObsession(entries, tracks, limit=PLAYLIST_SIZE * 20)["days"]
        seen, picked = set(), []
        for peak in peaks:   #< sorted by single-day plays; keep each song's best day
            tid = peak["trackId"]
            if tid in seen or not usable(tid):
                continue
            seen.add(tid)
            picked.append({"id": tid, "detail": f"{peak['plays']} plays on {peak.get('bucketText') or peak['bucket']}"})
            if len(picked) >= PLAYLIST_SIZE:
                break
        return {
            "key": "peak-obsessions",
            "name": "Peak obsessions",
            "description": f"The songs you played the most in a single day. {MARKER}",
            "tracks": picked,
        }

    raise GeneratorError("Unknown playlist type.")


def _intParam(params, name):
    value = (params or {}).get(name)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise GeneratorError(f"Invalid {name}.")


class PlaylistPublisher:
    """ Creates or updates the Spotify playlist for a generator key. Web requests: never waits on 429s. """

    def __init__(self, api, userFolder):
        self.api = api
        self.store = CachedJson(userFolder / "generated_playlists.json")
        self._lock = threading.Lock()

    def existing(self, key):
        return (self.store.load().get("playlists") or {}).get(key)

    def _myPlaylists(self):
        playlists, offset = [], 0
        while True:
            page = self.api.call(lambda c, o=offset: c._get("me/playlists", limit=50, offset=o)) or {}
            items = [p for p in page.get("items") or [] if p]
            playlists.extend(items)
            offset += len(items)
            if not items or not page.get("next"):
                return playlists

    def _findTarget(self, key, name, myId):
        """ The playlist to update: the one we made for this key, if it's still in your library. """
        known = self.existing(key)
        mine = [p for p in self._myPlaylists() if (p.get("owner") or {}).get("id") == myId]
        if known:
            for p in mine:
                if p.get("id") == known.get("id"):
                    return p["id"]
        for p in mine:   #< lost the record (e.g. new install): reuse one we clearly made, never a hand-made one
            if p.get("name") == (known or {}).get("name", name) and MARKER in (p.get("description") or ""):
                return p["id"]
        return None

    def publish(self, playlist):
        uris = [f"spotify:track:{t['id']}" for t in playlist["tracks"]]
        if not uris:
            raise GeneratorError("There are no songs to put in this playlist.")
        with self._lock:   #< double-clicks don't create two playlists
            me = self.api.call(lambda c: c._get("me")) or {}
            targetId = self._findTarget(playlist["key"], playlist["name"], me.get("id"))
            created = targetId is None
            if created:
                made = self.api.call(lambda c: c._post("me/playlists", payload={
                    "name": playlist["name"], "description": playlist["description"], "public": False})) or {}
                targetId = made.get("id")
                if not targetId:
                    raise GeneratorError("Spotify didn't return the new playlist.")
            self._remember(playlist, targetId, len(uris), complete=False)   #< so a retry updates, not duplicates
            if not created:
                self.api.call(lambda c: c._put(f"playlists/{targetId}", payload={
                    "name": playlist["name"], "description": playlist["description"]}))
            first, rest = uris[:ITEMS_PER_REQUEST], uris[ITEMS_PER_REQUEST:]
            self.api.call(lambda c: c._put(f"playlists/{targetId}/items", payload={"uris": first}))
            for i in range(0, len(rest), ITEMS_PER_REQUEST):
                chunk = rest[i:i + ITEMS_PER_REQUEST]
                self.api.call(lambda c, ch=chunk: c._post(f"playlists/{targetId}/items", payload={"uris": ch}))
            record = self._remember(playlist, targetId, len(uris), complete=True)
            return dict(record, created=created)

    def _remember(self, playlist, playlistId, count, complete):
        data = self.store.load()
        playlists = dict(data.get("playlists") or {})
        record = {
            "id": playlistId,
            "name": playlist["name"],
            "url": f"https://open.spotify.com/playlist/{playlistId}",
            "trackCount": count,
            "updatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "complete": complete,
        }
        playlists[playlist["key"]] = record
        self.store.save({"playlists": playlists})
        return record


def describeSpotifyError(error):
    if isinstance(error, SpotifyException):
        if error.http_status in (401, 403):
            return "Spotify refused the request. Try logging in again."
        return f"Spotify returned an error ({error.http_status}). Try again in a moment."
    return str(error)
