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
import time

from spotipy.exceptions import SpotifyException

try:
    from Database.spotifyCache import CachedJson, BACKGROUND_MAX_WAIT, RETRY_AFTER_FAILURE
    from Database.spotifyApi import RateLimitedError, SpotifyAuthError
    from Database.utils import convertToDatetime
    from Database import analytics
except ModuleNotFoundError:
    from spotifyCache import CachedJson, BACKGROUND_MAX_WAIT, RETRY_AFTER_FAILURE
    from spotifyApi import RateLimitedError, SpotifyAuthError
    from utils import convertToDatetime
    import analytics

PLAYLIST_SIZE = 50
CANDIDATES = PLAYLIST_SIZE * 3   #< ranked songs kept so unavailable ones can be replaced by the next ones
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
    Returns {"key", "name", "description", "candidates", "tracks"} for one generator.
    candidates: up to CANDIDATES ranked [{"id", "detail"}]; tracks: the first PLAYLIST_SIZE of them,
    before any availability check (see selectPlayable).
    kind: "top-year" (params: year), "forgotten" (params: year, optional), "peak-obsessions".
    """
    playlist = _generate(kind, params, entries, tracks, now)
    playlist["tracks"] = playlist["candidates"][:PLAYLIST_SIZE]
    playlist["summary"] = playlist["description"].replace(MARKER, "").strip()   #< for the page; Spotify gets the marker
    return playlist


def _generate(kind, params, entries, tracks, now=None):
    now = now or datetime.datetime.now().astimezone()
    usable = lambda tid: tid in tracks and SPOTIFY_ID.fullmatch(tid or "")

    if kind == "top-year":
        year = _intParam(params, "year")
        if year is None:
            raise GeneratorError("Pick a year.")
        start, end = _yearBounds(year)
        counts = _plays(entries, start, end)
        ids = [t for t in _ranked(counts) if usable(t)][:CANDIDATES]
        return {
            "key": f"top-year-{year}",
            "name": f"Top 50 of {year}",
            "description": f"Your most played songs of {year}. {MARKER}",
            "candidates": [{"id": t, "detail": f"{counts[t]} plays in {year}"} for t in ids],
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
               if recent.get(t, 0) <= FORGOTTEN_MAX_RECENT and usable(t)][:CANDIDATES]

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
            "candidates": [{"id": t, "detail": detail(t)} for t in ids],
        }

    if kind == "peak-obsessions":
        peaks = analytics.peakObsession(entries, tracks, limit=CANDIDATES * 10)["days"]
        seen, picked = set(), []
        for peak in peaks:   #< sorted by single-day plays; keep each song's best day
            tid = peak["trackId"]
            if tid in seen or not usable(tid):
                continue
            seen.add(tid)
            picked.append({"id": tid, "detail": f"{peak['plays']} plays on {peak.get('bucketText') or peak['bucket']}"})
            if len(picked) >= CANDIDATES:
                break
        return {
            "key": "peak-obsessions",
            "name": "Peak obsessions",
            "description": f"The songs you played the most in a single day. {MARKER}",
            "candidates": picked,
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


PLAYABILITY_MAX_AGE = 7 * 24 * 3600
CHECK_PAUSE = 0.1   #< seconds between single-track lookups (bulk lookups were removed in 2026)
UNAVAILABLE_TEXT = {
    "market": "unavailable in your region",
    "notFound": "no longer on Spotify",
}


def playabilityOf(track):
    """ GET /tracks/{id}?market=from_token -> {"playable", "reason"}. """
    restrictions = (track or {}).get("restrictions") or {}
    if track.get("is_playable") is False or restrictions:
        return {"playable": False, "reason": restrictions.get("reason") or "market"}
    return {"playable": True, "reason": None}


class PlayabilityChecker:
    """
    Checks, in a background thread, whether tracks can be played in your market, one request per
    track through the shared client. Results are cached in playability.json for a week.
    """

    def __init__(self, api, userFolder, pause=CHECK_PAUSE, maxAge=PLAYABILITY_MAX_AGE, clock=time.time):
        self.api = api
        self.cache = CachedJson(userFolder / "playability.json")
        self.pause = pause
        self.maxAge = maxAge
        self.clock = clock
        self._lock = threading.Lock()
        self._queue = []
        self._running = False
        self._pausedUntil = 0.0
        self.lastError = None
        self.thread = None

    def known(self, trackId):
        entry = (self.cache.load().get("tracks") or {}).get(trackId)
        if not entry or self.clock() - entry.get("checkedAt", 0) > self.maxAge:
            return None
        return entry

    def pausedUntil(self):
        return self._pausedUntil if self._pausedUntil > self.clock() else None

    def ensure(self, trackIds):
        """ Queue unknown tracks and start the checker if needed. Never blocks. """
        with self._lock:
            for tid in trackIds:
                if tid not in self._queue and self.known(tid) is None:
                    self._queue.append(tid)
            if self._running or not self._queue or self.pausedUntil() or not self.api.isConnected():
                return
            self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="playability")
        self.thread.start()

    def checkNow(self, trackIds):
        """ Synchronous variant (tests, tools). """
        self.ensure(trackIds)
        if self.thread:
            self.thread.join()

    def _run(self):
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    tid = self._queue[0]
                try:
                    track = self.api.call(lambda c: c._get(f"tracks/{tid}", market="from_token"), maxWait=BACKGROUND_MAX_WAIT)
                    result = playabilityOf(track or {})
                except SpotifyException as e:
                    if e.http_status in (400, 404):
                        result = {"playable": False, "reason": "notFound"}
                    else:
                        raise
                self._store(tid, result)
                with self._lock:
                    if tid in self._queue:
                        self._queue.remove(tid)
                time.sleep(self.pause)
        except RateLimitedError as e:
            self._pausedUntil = e.retryAt
            self.lastError = str(e)
            print(f"Availability check paused: {e}")
        except (SpotifyAuthError, SpotifyException, Exception) as e:
            self._pausedUntil = self.clock() + RETRY_AFTER_FAILURE
            self.lastError = f"{type(e).__name__}: {e}"
            print(f"Availability check failed (retrying in {RETRY_AFTER_FAILURE // 60} min): {self.lastError}")
        finally:
            with self._lock:
                self._running = False

    def _store(self, trackId, result):
        data = self.cache.load()
        tracks = dict(data.get("tracks") or {})
        tracks[trackId] = dict(result, checkedAt=self.clock())
        self.cache.save({"tracks": tracks})


def selectPlayable(playlist, checker, size=PLAYLIST_SIZE):
    """
    Walks the ranked candidates: keeps playable songs, skips unavailable ones and backfills from the
    next-ranked songs until there are `size`. Returns {"tracks", "skipped", "pending"}; while
    pending > 0 the list isn't final (the checker is still looking songs up).
    """
    chosen, skipped, unknown = [], [], []
    for candidate in playlist["candidates"]:
        if len(chosen) + len(unknown) >= size:
            break
        state = checker.known(candidate["id"])
        if state is None:
            unknown.append(candidate)
        elif state["playable"]:
            chosen.append(candidate)
        else:
            skipped.append(dict(candidate, unavailable=UNAVAILABLE_TEXT.get(state.get("reason"), "unavailable on your account")))
    if unknown:
        checker.ensure([c["id"] for c in unknown])
        # Show the songs still being checked in rank order, as if playable
        order = {c["id"]: i for i, c in enumerate(playlist["candidates"])}
        chosen = sorted(chosen + unknown, key=lambda c: order[c["id"]])
    return {"tracks": chosen, "skipped": skipped, "pending": len(unknown)}


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
                    "name": playlist["name"], "description": playlist["description"], "public": False}))
            first, rest = uris[:ITEMS_PER_REQUEST], uris[ITEMS_PER_REQUEST:]
            self.api.call(lambda c: c._put(f"playlists/{targetId}/items", payload={"uris": first}))
            for i in range(0, len(rest), ITEMS_PER_REQUEST):
                chunk = rest[i:i + ITEMS_PER_REQUEST]
                self.api.call(lambda c, ch=chunk: c._post(f"playlists/{targetId}/items", payload={"uris": ch}))
            self._ensurePrivate(targetId)
            record = self._remember(playlist, targetId, len(uris), complete=True)
            return dict(record, created=created)

    def _ensurePrivate(self, playlistId):
        """ Read the playlist back; if Spotify kept it public, set public=false again and warn. """
        state = self.api.call(lambda c: c._get(f"playlists/{playlistId}", fields="public")) or {}
        if state.get("public") is not False:
            print(f"Warning: playlist {playlistId} came back public={state.get('public')!r}; setting it to private")
            self.api.call(lambda c: c._put(f"playlists/{playlistId}", payload={"public": False}))

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
