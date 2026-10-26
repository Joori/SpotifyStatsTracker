# SpotifyStatsTracker (official Web API fork)

Fork of [kjljixx/SpotifyStatsTracker](https://github.com/kjljixx/SpotifyStatsTracker)
(itself a fork of [TzurSoffer/SpotifyStatsTracker](https://github.com/TzurSoffer/SpotifyStatsTracker)).

### Originally made by Tzur Soffer. If you found [the original repository](https://github.com/TzurSoffer/SpotifyStatsTracker) useful, please give it a ⭐!

**This fork uses the official Spotify Web API instead of SpotipyFree / browser cookies**,
so it stays within Spotify's Developer Terms. That changes the requirements (see below).

## What's new in 1.8.0

- **Profile:** your Spotify name and avatar in the sidebar.
- **Now playing:** a live card on the Overview, and in the dashboard API.
- **Spotify vs you:** Spotify's own top lists next to what the tracker counted (Insights).
- **Liked vs played:** liked songs you never play, and songs you play a lot but haven't liked (Insights).
- **Playlist Maker:** make Spotify playlists from your history.

Upgrading from 1.7: these need new Spotify permissions, so after updating the site shows
**Relog to Spotify**. Log in once to grant them; live tracking pauses until you do.

## Requirements

- **Spotify Premium.** Spotify requires a Premium account to own a Development Mode app.
- A Spotify Developer app (one per developer account, up to 5 authorised users).
- An **HTTPS** redirect URI (loopback addresses like `http://127.0.0.1` are the only exception),
  e.g. a reverse proxy in front of the tracker.

## Setup

1. Create an app at https://developer.spotify.com/dashboard, tick **Web API**.
2. Add your redirect URI, e.g. `https://tracker.example.com/callback`.
3. Create a `.env` next to `docker-compose.yml`:
       SPOTIFY_CLIENT_ID=...
       SPOTIFY_CLIENT_SECRET=...
       SPOTIFY_REDIRECT_URI=https://tracker.example.com/callback
4. `docker compose up -d --build`, open the site, click **Connect to Spotify**.

The tracker asks for these permissions (scopes):

| Scope | Used for |
| --- | --- |
| `user-read-recently-played` | Live tracking |
| `playlist-read-private` | Names of the playlists you played from |
| `user-read-currently-playing` | Now playing |
| `user-top-read` | Spotify vs you |
| `user-library-read` | Liked vs played |
| `playlist-modify-private` | Playlist Maker |

If an update adds a scope, the site shows **Relog to Spotify** until you log in again; live tracking
pauses until then.

To check which endpoints and fields your app can use, run `python tools/probeSpotifyApi.py`
(inside the container: `docker compose exec spotify-tracker python tools/probeSpotifyApi.py`).
It prints status codes and field names only. `--write` also creates one test playlist,
"SST probe (delete me)", which you delete by hand.

Optional env: `SPOTIFY_POLL_INTERVAL` (default 120s), `SPOTIFY_MAX_RETRY_WAIT` (default 300s),
`FLASK_DEBUG=1` (only when debugging; off by default).

## Limitations of the official API

- **Live plays have estimated listen time.** Recently-played doesn't report how long you listened,
  so live plays are stored at full track length and marked *(estimated)*. They're excluded from
  skip/completion stats. Importing a later extended-history export replaces them with the real values.
- **Imports are rate-limited.** Importing a large history makes one request per unique track
  and artist. Spotify can impose long (hours-long) cooldowns. The importer waits out short
  ones and stops with a resume time for long ones; re-running continues where it left off.
- **Re-login every ~6 months.** Spotify refresh tokens expire after six months; the app will show
  "Relog to Spotify" when that happens.
- **Spotify-generated playlists** (Daily Mix, On Repeat, etc.) can't be read by Development Mode apps,
  so plays from them show the context as "Spotify mix".
- **Top artists can be sparse.** Spotify sometimes returns few or no top artists for a period;
  Spotify vs you then says so instead of comparing.
- **Playlists show as "Public Playlist" in the Spotify app.** Playlists the tracker makes are kept
  off your profile, but the API can't set the app's own private setting (see Playlist Maker).
- **Song availability is checked one song at a time.** Development Mode apps can't look up
  several tracks in one request, so the first preview of a playlist takes a few seconds.

## Features

- Top songs, artists, albums and playlists for any time range
- Full listening history with search
- Live tracking: checks your recently played every 2 minutes
- How much of a song you get through on average before skipping
- Listening patterns by hour, weekday and month, plus streaks and sessions
- The days you discovered the most new music
- Graphs of how much you listened to a song over time
- Import your Spotify data export (account or extended history), several files at once
- Import Musicolet exports
- Auto-import files dropped into the `autoImport` folder, with optional filtering
- Your Spotify display name and avatar in the sidebar (refreshed daily)
- Now playing on the Overview: cover, artist and a progress bar
- Spotify vs you: Spotify's top artists and tracks next to the tracker's own counts
- Liked vs played: liked songs you rarely play, and favourites you haven't liked
- Playlist Maker: Spotify playlists made from your history
- A read-only JSON summary for home dashboards (see below)

Screenshots are in the `assets` folder.

## Running with Docker

The included `docker-compose.yml` builds the image from this repo. Don't use the upstream
`mepro3/spotify-tracker` image: it still uses SpotipyFree.

```bash
git clone https://github.com/Joori/SpotifyStatsTracker
cd SpotifyStatsTracker
# create .env as described in Setup, then:
docker compose up -d --build
```

The app listens on port **5462**. Point your reverse proxy at it, and use the proxy's HTTPS
address for `SPOTIFY_REDIRECT_URI`.

Set `TZ` in `docker-compose.yml` to your timezone, or play times will be off.

Folders on the host:

| Folder | Holds |
| --- | --- |
| `Database/` | Your plays, track info and images |
| `secrets/` | The Spotify login token. Keep it private |
| `autoImport/` | Files dropped here are imported automatically. Set `IMPORT_KEYWORD` to only import files whose name contains it |

To update: `git pull`, then `docker compose up -d --build`.

Your user folder (the folder under `Database/` that holds `entries.json`) also keeps these
caches. They can be deleted safely; the tracker fetches them again when needed.

| File | Holds | Refreshed |
| --- | --- | --- |
| `profile.json`, `img/profile/` | Your Spotify name and avatar | Daily |
| `spotify_top.json` | Spotify's top artists and tracks | Every 6 hours |
| `liked.json` | Your liked songs | Daily |
| `playability.json` | Which songs Spotify will play in your region | Weekly |
| `generated_playlists.json` | The playlist each Playlist Maker list created | When you create or update |

Deleting `generated_playlists.json` makes Playlist Maker create new playlists next time instead of
updating the old ones.

## Importing your history

Request your data at https://www.spotify.com/account/privacy. The **Extended streaming history**
gives exact listen times and skips. Upload the `Streaming_History_Audio_*.json` (or
`StreamingHistory*.json`) files on the **Import** page, or drop them into `autoImport/`.

Importing a newer export after the tracker has been running replaces the matching live
(estimated) plays with the real ones, so nothing is counted twice.

## Now playing

The Overview shows what's playing on your Spotify account: cover, title, artist and a progress
bar. It handles paused playback, podcast episodes, ads and nothing playing. Spotify is only asked
while an Overview tab is open and visible (every 20 seconds), never in the background.

## Spotify vs you

On **Insights**, compares Spotify's own top tracks and artists with what the tracker counted over
the same periods: last 4 weeks, last 6 months and last year. For each period it lists:

- **Spotify ranks high, you rarely play:** in Spotify's top 20, but 2 plays or fewer in the
  tracker (or outside your top 50).
- **You play a lot, Spotify doesn't list:** in your top 20, but not in Spotify's list.

Each period shows how many plays the tracker counted. If your history has gaps (for example
years before you imported or started tracking), Spotify's lists are based on listening the
tracker never saw, so the two will disagree.

## Liked vs played

Also on **Insights**, compares your Liked Songs with everything the tracker has counted:

- **Liked but rarely played:** liked songs with 2 plays or fewer, never-played ones first.
- **Heavily played but not liked:** songs with 10 or more plays that aren't in your Liked Songs.

Songs are matched by Spotify ID, then by ISRC, then by title and first artist, so a song Spotify
has re-released under a new ID still counts.

## Playlist Maker

Makes Spotify playlists from your history:

- **Top 50 of a year:** your 50 most played songs of the year you pick.
- **Forgotten favourites:** songs you played 10+ times back then (any time before the last
  12 months, or a year you pick) but 2 times or fewer in the last 12 months.
- **Peak obsessions:** the 50 songs you played the most in a single day.

Pick one and press **Preview** to see the list. Nothing changes on Spotify until you press
**Create** (or **Update**). Running the same list again updates its playlist instead of making a
duplicate; if you've removed it from your library, a new one is made.

Songs Spotify won't play in your region are skipped and listed as "unavailable in your region",
and the next songs fill their places so the playlist still has 50. Checking takes a few seconds
the first time; the page updates itself while it checks.

**Privacy:** playlists are created with "public" off, so they don't appear on your profile. The
Spotify app still labels them "Public Playlist", so anyone with the link can open them; the API
can't change that. To make one fully private, open it in Spotify and choose **Make private**.

## Playlist names and covers

Spotify-made playlists can't be read through the API, so they show as "Spotify mix" with the
cover of the song you played most from them. To give one (or any playlist) its own name or
cover, create `playlist_overrides.json` in your user folder: the folder under `Database/` on the
host that holds `entries.json`.

```json
{
  "37i9dQZF1EIebRCzNzc6XZ": { "name": "Daily Mix 1", "cover": "daily-mix-1.jpg" },
  "5tNJ1RhtIm1kI7EMiJMDOr": { "name": "Bush Doof" }
}
```

- The key is the playlist ID: the part after `/playlist/` in its Spotify link or in the tracker's playlist page URL.
- `name` and `cover` are both optional. Overrides win over names from Spotify and over "Spotify mix".
- Put cover images in `img/playlists/` inside the same user folder. `cover` is the bare filename.
- Changes apply on the next page load, with no restart or re-import. If the file isn't valid JSON,
  it's ignored and a message is logged.

Without an override or a usable cover, a generic playlist icon is shown.

## Dashboard API

`GET /api/summary` returns a small read-only JSON summary, e.g. for a
[Homepage](https://gethomepage.dev) `customapi` widget. It contains no tokens or credentials.

```json
{
  "plays_today": 12, "listen_seconds_today": 2710,
  "plays_week": 140, "total_plays": 27760,
  "current_streak_days": 4,
  "last_played": {"track": "Pratigi", "artist": "Vegas (Brazil)", "played_at": "2026-10-26T01:03:35+11:00"},
  "listener": {"connected": true, "last_poll": "2026-10-26T09:14:02+11:00"},
  "now_playing": {"is_playing": true, "type": "track", "track": "Pratigi", "artist": "Vegas (Brazil)",
                  "album": "Pratigi", "progress_ms": 61000, "duration_ms": 347000}
}
```

"Today" and "week" mean the last 24 hours and the last 7 days, the same ranges as the Overview page.
`now_playing` is `null` when nothing is playing. `type` is `track`, `episode`, `ad` or `unknown`
(ads and unknown items have no track details). It is checked when the summary is requested and
reused for 10 seconds; `GET /api/now-playing` returns the same data in more detail.

## Running without Docker

```bash
pip install -r requirements.txt
export SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=...
export SPOTIFY_REDIRECT_URI=http://127.0.0.1:5462/callback   # loopback, so plain HTTP is allowed
python app.py
```

Then open http://127.0.0.1:5462. Add the same redirect URI to your Spotify app.

## License

This project is licensed under the `Soffer Non-Commercial Source-Available License 1.1`. See the LICENCE file for details.

## Support

For problems with this fork, open an issue at https://github.com/Joori/SpotifyStatsTracker/issues.
When reporting a bug, restart with `FLASK_DEBUG=1` and include the logs.
