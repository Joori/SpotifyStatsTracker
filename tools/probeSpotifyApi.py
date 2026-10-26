"""
Probe the Spotify Web API endpoints the tracker's features need, once each, with the stored token.
Prints the HTTP status and which fields come back (field names and counts only: no tokens, no values).

    python tools/probeSpotifyApi.py            # read-only endpoints
    python tools/probeSpotifyApi.py --write    # also creates ONE private test playlist and edits it

The --write check leaves the playlist "SST probe (delete me)" in your library; delete it by hand.
Run it where the tracker's secrets/ folder is (e.g. inside the container: /app).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spotipy.exceptions import SpotifyException  # noqa: E402
from Database.spotifyApi import SpotifyApi, SpotifyAuthError, RateLimitedError  # noqa: E402

PROBE_PLAYLIST_NAME = "SST probe (delete me)"
MAX_WAIT = 60   #< wait out short 429s; longer ones stop the probe


def describe(value, depth=0, maxDepth=2):
    """ Field names (nested a couple of levels) without any values. """
    if isinstance(value, dict):
        if depth >= maxDepth:
            return "{" + ", ".join(sorted(value)) + "}"
        parts = []
        for key in sorted(value):
            sub = value[key]
            if isinstance(sub, (dict, list)) and sub:
                parts.append(f"{key}: {describe(sub, depth + 1, maxDepth)}")
            elif sub is None:
                parts.append(f"{key}=null")
            else:
                parts.append(key)
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{len(value)} x {describe(value[0], depth, maxDepth)}]"
    return type(value).__name__


class Probe:
    def __init__(self, api, out=print):
        self.api = api
        self.out = out
        self.results = []   #< (label, ok, status)

    def run(self, label, fn, show=True, maxDepth=2):
        try:
            data = self.api.call(fn, maxWait=MAX_WAIT)
        except SpotifyException as e:
            reason = (e.msg or "").splitlines()[-1].strip() if e.msg else ""
            self.out(f"FAIL {e.http_status}  {label}: {reason}")
            self.results.append((label, False, e.http_status))
            return None
        if data is None:
            self.out(f"OK   204  {label}: no content")
            self.results.append((label, True, 204))
            return None
        self.out(f"OK   200  {label}")
        if show:
            self.out(f"          fields: {describe(data, maxDepth=maxDepth)}")
        self.results.append((label, True, 200))
        return data

    def summary(self):
        failed = [r for r in self.results if not r[1]]
        self.out("")
        self.out(f"{len(self.results) - len(failed)}/{len(self.results)} calls succeeded")
        for label, _, status in failed:
            self.out(f"  failed: {label} ({status})")
        return failed


def _firstTrackUris(*sources, count=2):
    uris = []
    for source in sources:
        for item in (source or {}).get("items") or []:
            track = item.get("track") if "track" in item else item
            uri = (track or {}).get("uri")
            if uri and uri.startswith("spotify:track:") and uri not in uris:
                uris.append(uri)
            if len(uris) >= count:
                return uris
    return uris


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description="Probe the Spotify endpoints the tracker uses.")
    parser.add_argument("--write", action="store_true",
                        help=f'also create one private playlist "{PROBE_PLAYLIST_NAME}" and edit it')
    parser.add_argument("--token-cache", default=str(Path(__file__).resolve().parent.parent / "secrets" / ".spotify_token_cache"))
    args = parser.parse_args(argv)

    api = SpotifyApi(args.token_cache)
    missing = api.cachedMissingScopes()
    if missing:
        out(f"Stored token lacks scopes: {' '.join(missing)}. Relog in the tracker first.")
        return 2
    probe = Probe(api, out)
    try:
        probe.run("GET /me", lambda c: c._get("me"))
        probe.run("GET /me/player/currently-playing", lambda c: c._get("me/player/currently-playing", additional_types="track,episode"))
        topTracks = None
        for timeRange in ("short_term", "medium_term", "long_term"):
            probe.run(f"GET /me/top/artists ({timeRange})", lambda c, t=timeRange: c._get("me/top/artists", time_range=t, limit=50), maxDepth=3)
            data = probe.run(f"GET /me/top/tracks ({timeRange})", lambda c, t=timeRange: c._get("me/top/tracks", time_range=t, limit=50), maxDepth=3)
            topTracks = topTracks or data
        liked = probe.run("GET /me/tracks", lambda c: c._get("me/tracks", limit=50), maxDepth=3)
        if liked is not None:
            out(f"          total liked songs: {liked.get('total')}")
        probe.run("GET /me/playlists", lambda c: c._get("me/playlists", limit=5))
        recent = probe.run("GET /me/player/recently-played", lambda c: c._get("me/player/recently-played", limit=5), show=False)

        if args.write:
            uris = _firstTrackUris(topTracks, liked, recent)
            if len(uris) < 2:
                out("Skipping write checks: couldn't find two track URIs to use.")
            else:
                created = probe.run("POST /me/playlists", lambda c: c._post("me/playlists", payload={
                    "name": PROBE_PLAYLIST_NAME, "public": False,
                    "description": "Created by SpotifyStatsTracker's API probe. Safe to delete."}))
                playlistId = (created or {}).get("id")
                if playlistId:
                    probe.run("POST /playlists/{id}/items", lambda c: c._post(f"playlists/{playlistId}/items", payload={"uris": uris[:1]}))
                    probe.run("PUT /playlists/{id}/items (replace)", lambda c: c._put(f"playlists/{playlistId}/items", payload={"uris": uris[1:2]}))
                    probe.run("PUT /playlists/{id} (details)", lambda c: c._put(f"playlists/{playlistId}", payload={
                        "description": "Created by SpotifyStatsTracker's API probe. Safe to delete. (updated)"}))
                    probe.run("GET /playlists/{id}", lambda c: c._get(f"playlists/{playlistId}", fields="id,name,public,owner(id),items(total)"))
                    probe.run("GET /playlists/{id}/items", lambda c: c._get(f"playlists/{playlistId}/items", limit=5), maxDepth=3)
                    out(f'Left the private playlist "{PROBE_PLAYLIST_NAME}" in your library; delete it by hand.')
    except SpotifyAuthError as e:
        out(f"Spotify login problem ({e.reason}). Relog in the tracker and run again.")
        return 2
    except RateLimitedError as e:
        out(f"Stopped: {e}")
        probe.summary()
        return 3
    return 1 if probe.summary() else 0


if __name__ == "__main__":
    sys.exit(main())
