import os
import secrets
import threading
import requests
from pathlib import Path
from urllib.parse import urlparse
import time
from datetime import datetime, timedelta, date as date_cls

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory

from Database.database import Database
from Database import analytics as analyticsLib
from Database.Migrators.migrate import migrateIfNeeded
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString, versionTuple, now, parseDateString
from Database.spotifyApi import SpotifyApi
from spotipy.exceptions import SpotifyOauthError

EXPLORE_TABS = (
    {"key": "songs", "label": "Songs", "section": "top_songs"},
    {"key": "artists", "label": "Artists", "section": "top_artists"},
    {"key": "albums", "label": "Albums", "section": "top_albums"},
    {"key": "contexts", "label": "Playlists", "section": "top_contexts"},
)

class SpotifyDashboardApp:
    def __init__(self):
        migrateIfNeeded()
        self.app = Flask(__name__)
        self.baseDir = Path(__file__).resolve().parent
        self.username = "kjljixx"
        self.spotify = SpotifyApi(self.baseDir / "secrets" / ".spotify_token_cache")
        self.pendingOAuthStates = {}          #< state -> (created, next path); single-user app, kept in memory
        self.database = Database(user=self.username, spotifyApi=self.spotify)
        self.database.startAutoImporter()
        self.database.resetProgress()
        try:
            self.currentVersion = (self.baseDir / "Database" / "VERSION").read_text(encoding="utf-8").strip()  #< only needs to be checked once because app cant update without restart
        except Exception:
            self.currentVersion = "0.0.0"
        self.latestVersion = None
        self._version_lock = threading.Lock()
        self.startVersionCheck_thread()
        self.checkLogin_thread()

        self.registerRoutes()

    @property
    def isLoggedIn(self):
        return self.spotify.isConnected()

    def startListenerIfNeeded(self):
        if self.database.listener is None and self.spotify.isConnected():
            self.database.startListener()
            print("Started listener thread.")

    def checkLogin_thread(self):
        thread = threading.Thread(target=self._checkLoginLoop, daemon=True)
        thread.start()

    def _ensureLogin(self):
        self.spotify.checkToken()   #< refreshes the token if needed; no API calls, never waits on rate limits
        self.startListenerIfNeeded()

    def _checkLoginLoop(self):
        while True:
            try:
                self._ensureLogin()
            except Exception as e:
                print(f"Login check failed: {e}")
            time.sleep(60 * 5)  # Check every 5 minutes

    def _newOAuthState(self, nextPath):
        now_ = time.time()
        self.pendingOAuthStates = {k: v for k, v in self.pendingOAuthStates.items() if now_ - v[0] < 600}
        state = secrets.token_urlsafe(24)
        self.pendingOAuthStates[state] = (now_, nextPath)
        return state

    @staticmethod
    def _safeNextPath(nextPath):
        if nextPath and nextPath.startswith("/") and not nextPath.startswith("//"):
            return nextPath
        return None

    def startVersionCheck_thread(self):
        thread = threading.Thread(target=self._versionCheckLoop, daemon=True)
        thread.start()

    def _versionCheckLoop(self):
        # Check version from GitHub at startup and then every hour.
        url = "https://raw.githubusercontent.com/TzurSoffer/SpotifyStatsTracker/main/Database/VERSION"
        while True:
            try:
                resp = requests.get(url, timeout=6)
                if resp.status_code == 200:
                    remoteVersion = resp.text.strip()
                    # store remoteVersion if it's newer than current
                    try:
                        with self._version_lock:
                            if versionTuple(remoteVersion) > versionTuple(self.currentVersion):
                                self.latestVersion = remoteVersion
                            else:
                                self.latestVersion = None
                    except:
                        pass
            except Exception:
                pass

            time.sleep(60 * 60)

    def _getPercentPlayedText(self, item, sortBy, totalPlays, totalMs):
        if sortBy == "plays":
            percent = round((item.get("plays", 0) / totalPlays * 100), 1) if totalPlays else 0
            return f"{percent}% of all plays"
        elif sortBy == "totalTimeListened":
            percent =  round((item.get("totalTimeListened", 0) / totalMs * 100), 1) if totalMs else 0
            return f"{percent}% of all time played"
        else:
            return ""

    def _normalizeContextId(self, contextId: str, defaultType: str = "playlist") -> tuple[str, str]:
        """Return (fullUri, bareId) for a context given with or without its `type:` prefix."""
        raw = str(contextId or "").strip()
        if ":" in raw:
            contextType, bareId = raw.split(":", 1)
        else:
            contextType, bareId = defaultType, raw
        return f"{contextType}:{bareId}", bareId

    def _contextMatches(self, playedFrom, fullUri: str, bareId: str) -> bool:
        if not playedFrom:
            return False
        if playedFrom in (fullUri, bareId):
            return True
        return str(playedFrom).split(":", 1)[-1] == bareId

    def _safePlaylistName(self, playedFrom) -> str | None:
        try:
            return self.database.playlistName(playedFrom)
        except Exception:
            return None

    def _getContextUrl(self, playedFrom) -> str | None:
        if not playedFrom:
            return None
        fullUri, bareId = self._normalizeContextId(playedFrom)
        if not bareId:
            return None
        if fullUri.split(":", 1)[0] == "album":
            return f"/album/{bareId}"
        return f"/playlist/{fullUri}"

    def _getCompletionPercent(self, timePlayed, duration) -> float | None:
        if not duration or duration <= 0 or timePlayed is None:
            return None
        return min(round(timePlayed / duration * 100, 1), 100.0)

    def _embedSongTextElements(self, song) -> dict:
        if "playedAt" in song:   #< some tracks just dont have it (top tracks)
            playedAt = convertToDatetime(song["playedAt"])
            song["playedAtText"] = playedAt.strftime("%Y-%m-%d %H:%M")
            song["timePlayedText"] = msToString(song["timePlayed"])
            if song.get("estimated"):
                song["timePlayedText"] = f"~{song['timePlayedText']} (estimated)"

        song["contextName"] = None
        song["contextUrl"] = None
        playedFrom = song.get("playedFrom")
        if playedFrom:
            song["contextName"] = self._safePlaylistName(playedFrom)
            song["contextUrl"] = self._getContextUrl(playedFrom)

        completionPercent = None
        if not song.get("estimated"):
            completionPercent = self._getCompletionPercent(song.get("timePlayed"), song.get("duration"))
        song["completionPercent"] = completionPercent
        song["completionPercentText"] = f"{completionPercent}%" if completionPercent is not None else None

        artists = song.get("artists") or []
        if not isinstance(artists, list):
            artists = []
        artistsText = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))

        album = song.get("album") or {}
        if not isinstance(album, dict):
            album = {"name": str(album)}
        song["album"] = album

        releaseDate = album.get("releaseDate")
        releaseDateText = dateToString(releaseDate) if releaseDate else ""
        song["releaseDateText"] = releaseDateText
        song["artistsText"] = artistsText
        song["durationText"] = formatDuration(song.get("duration", 0))
        album["releaseDateText"] = releaseDateText

        albumId = str(album.get("id") or "")
        albumUrl = f"/album/{albumId}" if albumId and albumId != "0" else None
        album["detailUrl"] = albumUrl
        song["albumUrl"] = albumUrl
        return song

    def _embedTopSongTextElements(self, song, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        song["totalTimeListenedText"] = msToString(song.get("totalTimeListened", 0))
        song["firstListenedText"] = convertToDatetime(song.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        song["sortPercentText"] = self._getPercentPlayedText(song, sortBy, totalPlays, totalMs)
        song["detailUrl"] = f"/song/{song.get('id')}" if song.get("id") else None

        duration = song.get("duration") or 0
        plays = song.get("measuredPlays", song.get("plays")) or 0   #< estimated (live-recorded) plays don't count towards completion
        song["completionPercent"] = None
        song["completionPercentText"] = None
        if duration > 0 and plays > 0:   #< aggregated cards need completion across every play, not just the first one
            completionPercent = self._getCompletionPercent(song.get("measuredTimeListened", song.get("totalTimeListened", 0)), duration * plays)
            song["completionPercent"] = completionPercent
            song["completionPercentText"] = f"{completionPercent}%" if completionPercent is not None else None
        return song

    def _embedArtistTextElement(self, artist, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        artist["totalTimeListenedText"] = msToString(artist.get("totalTimeListened", 0))
        artist["firstListenedText"] = convertToDatetime(artist.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        artist["sortPercentText"] = self._getPercentPlayedText(artist, sortBy, totalPlays, totalMs)
        artist["detailUrl"] = f"/artist/{artist.get('id')}" if artist.get("id") else None
        return artist

    def _embedAlbumTextElements(self, album, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        album["totalTimeListenedText"] = msToString(album.get("totalTimeListened", 0))
        album["firstListenedText"] = convertToDatetime(album.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        album["sortPercentText"] = self._getPercentPlayedText(album, sortBy, totalPlays, totalMs)
        album["detailUrl"] = f"/album/{album.get('id')}" if album.get("id") else None
        albumDepth = album.get("albumDepth")
        album["albumDepthText"] = f"{albumDepth}% of the album" if albumDepth else None
        return album

    PLAYLIST_PLACEHOLDER = "/static/images/playlist-placeholder.svg"

    def _contextCoverUrl(self, contextUri, imageId=None) -> str:
        """ Cover for a playlist/album context: override cover > most-played track's cover > placeholder. """
        coverFile = self.database.playlistCoverFile(contextUri) if hasattr(self.database, "playlistCoverFile") else None
        if coverFile:
            return f"/img/{getattr(self, 'username', '')}/playlists/{coverFile}"
        if imageId:
            return f"/img/{getattr(self, 'username', '')}/tracks/{imageId}.jpeg"
        return self.PLAYLIST_PLACEHOLDER

    def _embedContextTextElements(self, context, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        context["totalTimeListenedText"] = msToString(context.get("totalTimeListened", 0))
        context["firstListenedText"] = convertToDatetime(context.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        context["sortPercentText"] = self._getPercentPlayedText(context, sortBy, totalPlays, totalMs)
        context["detailUrl"] = self._getContextUrl(context.get("id"))
        context["contextTypeText"] = "Album" if context.get("contextType") == "album" else "Playlist"
        context["coverUrl"] = self._contextCoverUrl(context.get("id"), context.get("imageId"))
        return context

    def _embedSongsTextElements(self, songs) -> list[dict]:
        return [self._embedSongTextElements(song) for song in songs]

    def _embedTopSongsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedTopSongTextElements(song, sortBy, totalPlays, totalMs) for song in songs]

    def _embedArtistsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedArtistTextElement(song, sortBy, totalPlays, totalMs) for song in songs]

    def _embedAlbumsTextElements(self, albums, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedAlbumTextElements(album, sortBy, totalPlays, totalMs) for album in albums]

    def _embedContextsTextElements(self, contexts, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedContextTextElements(context, sortBy, totalPlays, totalMs) for context in contexts]

    def _embedActivityPeriodTextElements(self, activityPeriods, every: int = 24) -> list[dict]:
        embedded = []
        for periodStart, periodEnd, totalTimeMs, playCount in activityPeriods:
            startDate = convertToDatetime(periodStart)
            endDate = convertToDatetime(periodEnd)
            if every <= 1:
                dateText = startDate.strftime("%H:%M")
                tooltipDate = f"{startDate.strftime('%A, %b %d, %Y %H:%M')} - {endDate.strftime('%H:%M')}"
            elif every < 24 * 7:
                dateText = startDate.strftime("%b %d")
                tooltipDate = f"{startDate.strftime('%A, %b %d, %Y')} - {endDate.strftime('%A, %b %d, %Y')}"
            elif every < 24 * 30:
                dateText = startDate.strftime("Week of %b %d")
                tooltipDate = f"{startDate.strftime('%A, %b %d, %Y')} - {endDate.strftime('%A, %b %d, %Y')}"
            elif every < 24 * 365:
                dateText = startDate.strftime("%b %Y")
                tooltipDate = f"{startDate.strftime('%B %Y')} - {endDate.strftime('%B %Y')}"
            else:
                dateText = startDate.strftime("%Y")
                tooltipDate = f"{startDate.strftime('%B %Y')} - {endDate.strftime('%B %Y')}"

            embedded.append({
                "date": dateToString(startDate),
                "dateText": dateText,
                "tooltipDate": tooltipDate,
                "playCount": playCount,
                "totalTimeMs": totalTimeMs,
                "totalTimeText": msToString(totalTimeMs),
            })
        return embedded

    def _getActivityBarGrouping(self, interval: str, startDate, endDate) -> tuple[int, str]:
        if interval == "day":
            return 1, "Hourly activity"
        if interval == "week":
            return 24, "Daily activity"
        if interval == "month":
            return 24 * 7, "Weekly activity"
        if interval == "year":
            return 24 * 30, "Monthly activity"
        if interval in ("5years", "all time"):
            return 24 * 365, "Yearly activity"

        if startDate and endDate:
            spanDays = max(1, int((endDate - startDate).total_seconds() // 86400))
            if spanDays <= 1:
                return 1, "Hourly activity"
            if spanDays <= 7:
                return 24, "Daily activity"
            if spanDays <= 31:
                return 24 * 7, "Weekly activity"
            if spanDays <= 365:
                return 24 * 30, "Monthly activity"

        return 24, "Daily activity"

    def _buildPageUrl(self, endpoint, page, **queryArgs):
        cleanArgs = {key: value for key, value in queryArgs.items() if value not in (None, "")}
        cleanArgs["page"] = page
        return url_for(endpoint, **cleanArgs)

    def _getNeighboringUrls(self, name, page, totalPages, **queryArgs):
        prevUrl = self._buildPageUrl(name, page - 1, **queryArgs) if page > 1 else None
        nextUrl = self._buildPageUrl(name, page + 1, **queryArgs) if page < totalPages else None
        return prevUrl, nextUrl

    def _normalizeSearchQuery(self, query: str | None) -> str:
        return (query or "").strip().lower()

    def _getSearchableText(self, item: dict) -> str:
        parts = [
            item.get("name", ""),
            item.get("artistsText", ""),
            item.get("artist", ""),
            item.get("contextName", ""),
            item.get("album", {}).get("name", "") if type(item.get("album")) == dict else "",
        ]

        artists = item.get("artists", [])
        for artist in artists:
            if type(artist) == dict:
                parts.append(artist.get("name", ""))
            else:
                parts.append(str(artist))

        playedFrom = item.get("playedFrom")
        if playedFrom:
            try:
                parts.append(self.database.playlistName(playedFrom))
            except:
                pass

        return " ".join(str(part) for part in parts if part)

    def _filterBySearch(self, items, query):
        normalizedQuery = self._normalizeSearchQuery(query)
        if not normalizedQuery:
            return items

        filtered = []
        for item in items:
            searchableText = self._getSearchableText(item).lower()
            if normalizedQuery in searchableText:
                filtered.append(item)
        return filtered

    def _getTotal(self, arr, key):
        return sum(i.get(key, 0) for i in arr)

    def _embedIndices(self, items):
        for index, item in enumerate(items, start=1):
            item["absoluteIndex"] = index
        return items

    def _getChangeText(self, currentValue, previousValue):
        if previousValue is None or previousValue == 0:
            if currentValue == 0:
                return None, ""
            return f"New this period", "change-positive"

        change = ((currentValue - previousValue) / previousValue) * 100
        formatted = f"{abs(round(change, 1))}% {'more' if change > 0 else 'less'} than the previous period"
        cssClass = "change-positive" if change > 0 else "change-negative"
        return formatted, cssClass

    def _getSpotifyEmbedUrl(self, entityType: str, entityData) -> str | None:
        if not entityData:
            return None

        if entityType == "playlist":
            if isinstance(entityData, str) and entityData.startswith("http"):
                return entityData.replace("https://open.spotify.com/playlist/", "https://open.spotify.com/embed/playlist/")
            if isinstance(entityData, str) and entityData.startswith("playlist:"):
                playlistId = entityData.split(":", 1)[-1]
                return f"https://open.spotify.com/embed/playlist/{playlistId}"
            return None

        if isinstance(entityData, str) and entityData.startswith("http"):
            identifier = entityData.rstrip("/").split("/")[-1]
            return f"https://open.spotify.com/embed/{entityType}/{identifier}"

        if isinstance(entityData, str):
            return f"https://open.spotify.com/embed/{entityType}/{entityData}"

        return None

    def _buildSparkline(self, buckets, key: str = "plays", width: int = 100, height: int = 30) -> dict:
        """Turn a timeline into svg polyline points so templates can draw it without JS."""
        values = [bucket.get(key, 0) for bucket in buckets]
        if not values:
            return {"points": "", "values": [], "max": 0, "width": width, "height": height}

        maxValue = max(values) or 1
        step = width / (len(values) - 1) if len(values) > 1 else 0
        points = " ".join(
            f"{round(index * step, 2)},{round(height - (value / maxValue) * height, 2)}"
            for index, value in enumerate(values)
        )
        return {"points": points, "values": values, "max": max(values), "width": width, "height": height}

    def _buildTimelineChart(self, timeline, title: str = "Listening over time") -> dict | None:
        if not timeline:
            return None

        xLabels = []
        for bucket in timeline:
            raw = bucket.get("date")
            label = bucket.get("label") or ""
            if raw:
                try:
                    text = str(raw)
                    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
                        xLabels.append(date_cls.fromisoformat(text[:10]).strftime("%b %d, %Y"))
                        continue
                    xLabels.append(convertToDatetime(raw).strftime("%b %d, %Y"))
                    continue
                except Exception:
                    pass
            xLabels.append(label or str(raw or ""))

        plays = [bucket.get("plays", 0) for bucket in timeline]
        layout = self._buildPlotlyLayout(title, "Plays")
        layout["margin"] = {"l": 48, "r": 16, "t": 16, "b": 56}
        layout["height"] = 360
        layout["xaxis"]["nticks"] = min(12, max(4, len(xLabels)))
        return {
            "data": [{
                "type": "scatter",
                "mode": "lines",
                "x": xLabels,
                "y": plays,
                "fill": "tozeroy",
                "line": {"color": "#3dd68c", "width": 2.5, "shape": "spline"},
                "fillcolor": "rgba(61, 214, 140, 0.14)",
                "hovertemplate": "%{x}<br>%{y} plays<extra></extra>",
            }],
            "layout": layout,
        }

    def _buildPlotlyLayout(self, title: str, yTitle: str = None, xTitle: str = None) -> dict:
        layout = {
            "title": {"text": title, "font": {"size": 15}},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "font": {"color": "#c7ccd1", "family": "Figtree, sans-serif"},
            "margin": {"l": 56, "r": 20, "t": 52, "b": 52},
            "xaxis": {"gridcolor": "rgba(255,255,255,0.06)", "zerolinecolor": "rgba(255,255,255,0.12)"},
            "yaxis": {"gridcolor": "rgba(255,255,255,0.06)", "zerolinecolor": "rgba(255,255,255,0.12)"},
            "hoverlabel": {
                "bgcolor": "#1c2126",
                "bordercolor": "rgba(255,255,255,0.18)",
                "font": {"color": "#f2f4f5", "family": "Figtree, sans-serif", "size": 13},
            },
            "showlegend": False,
        }
        if yTitle:
            layout["yaxis"]["title"] = {"text": yTitle}
        if xTitle:
            layout["xaxis"]["title"] = {"text": xTitle}
        return layout

    def _buildBarChart(self, labels, values, title, yTitle="Plays", hoverText=None, color="#1db954") -> dict:
        trace = {
            "type": "bar",
            "x": list(labels),
            "y": list(values),
            "marker": {"color": color, "line": {"width": 0}},
            "textposition": "none",
        }
        if hoverText:
            trace["hovertext"] = list(hoverText)
            trace["hovertemplate"] = "%{x}<br>%{y}<br>%{hovertext}<extra></extra>"
        else:
            trace["hovertemplate"] = "%{x}<br>%{y}<extra></extra>"
        return {"data": [trace], "layout": self._buildPlotlyLayout(title, yTitle)}

    def _buildMetricBarPair(self, labels, plays, ms, title, color="#3dd68c") -> dict:
        playsChart = self._buildBarChart(labels, plays, title, yTitle="Plays", color=color)
        msChart = self._buildBarChart(
            labels,
            [round(value / 60000, 1) for value in ms],
            title,
            yTitle="Minutes",
            hoverText=[msToString(value) for value in ms],
            color=color,
        )
        return {
            "data": playsChart["data"],
            "layout": playsChart["layout"],
            "dataByMetric": {
                "plays": playsChart["data"],
                "ms": msChart["data"],
            },
            "layoutByMetric": {
                "plays": playsChart["layout"],
                "ms": msChart["layout"],
            },
        }

    def _buildVarietyChart(self, monthlyStory) -> dict:
        labels = [month.get("label", "") for month in monthlyStory]
        layout = self._buildPlotlyLayout("Variety over time", "Unique count")
        layout["showlegend"] = True
        layout["legend"] = {"orientation": "h", "y": -0.25}
        return {
            "data": [
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "name": "Unique songs",
                    "x": labels,
                    "y": [month.get("uniqueSongs", 0) for month in monthlyStory],
                    "line": {"color": "#3dd68c", "width": 2},
                    "hovertemplate": "%{x}<br>%{y} unique songs<extra></extra>",
                },
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "name": "Unique artists",
                    "x": labels,
                    "y": [month.get("uniqueArtists", 0) for month in monthlyStory],
                    "line": {"color": "#6ab7ff", "width": 2},
                    "hovertemplate": "%{x}<br>%{y} unique artists<extra></extra>",
                },
            ],
            "layout": layout,
        }

    def _buildInsightCharts(self, timePatterns, monthlyStory) -> dict:
        hour = timePatterns.get("hour", {})
        dow = timePatterns.get("dow", {})
        month = timePatterns.get("month", {})
        return {
            "hour": self._buildMetricBarPair(
                hour.get("labels", []),
                hour.get("plays", []),
                hour.get("ms", []),
                "When you listen (hour of day)",
            ),
            "dow": self._buildMetricBarPair(
                dow.get("labels", []),
                dow.get("plays", []),
                dow.get("ms", []),
                "When you listen (day of week)",
                color="#6ab7ff",
            ),
            "month": self._buildMetricBarPair(
                month.get("labels", []),
                month.get("plays", []),
                month.get("ms", []),
                "When you listen (month of year)",
                color="#f7b955",
            ),
            "variety": self._buildVarietyChart(monthlyStory),
        }

    def _formatWeekKey(self, weekKey) -> str | None:
        if not weekKey or "-W" not in str(weekKey):
            return None
        try:
            from Database.analytics import _weekRangeLabel
            return _weekRangeLabel(weekKey)
        except Exception:
            year, week = str(weekKey).split("-W", 1)
            return f"Week {int(week)}, {year}"

    def _formatDayKey(self, dayKey) -> str | None:
        if not dayKey:
            return None
        try:
            if hasattr(dayKey, "strftime"):
                return dayKey.strftime("%b %d, %Y")
            text = str(dayKey)
            if len(text) == 10 and text[4] == "-" and text[7] == "-":
                year, month, day = text.split("-")
                return date_cls(int(year), int(month), int(day)).strftime("%b %d, %Y")
            return convertToDatetime(dayKey).strftime("%b %d, %Y")
        except Exception:
            return str(dayKey)

    def _buildSongDetailContext(self, songId: str):
        tracks = self.database._loadTracks() or {}
        track = tracks.get(songId)
        if not track:
            return None

        entries = [entry for entry in (self.database._loadEntries() or []) if entry.get("id") == songId]
        historyItems = []
        for entry in entries:
            historyItem = dict(track)
            historyItem["playedAt"] = entry.get("playedAt")
            historyItem["timePlayed"] = entry.get("timePlayed", 0)
            historyItem["playedFrom"] = entry.get("playedFrom")
            historyItems.append(self._embedSongTextElements(historyItem))

        historyItems.sort(key=lambda item: item.get("playedAt", 0), reverse=True)

        totalTimeMs = sum(item.get("timePlayed", 0) for item in historyItems)
        plays = len(historyItems)
        firstPlayedAt = min((item.get("playedAt", 0) for item in historyItems), default=0)
        lastPlayedAt = max((item.get("playedAt", 0) for item in historyItems), default=0)

        entity = self._embedSongTextElements(dict(track))
        entity["spotifyEmbedUrl"] = self._getSpotifyEmbedUrl("track", track.get("url"))
        entity["playCount"] = plays
        entity["totalTimeMs"] = totalTimeMs
        entity["firstListenedText"] = convertToDatetime(firstPlayedAt).strftime("%b %d, %Y") if firstPlayedAt else "Never"
        entity["lastListenedText"] = convertToDatetime(lastPlayedAt).strftime("%b %d, %Y") if lastPlayedAt else "Never"

        completionRate = self.database.getCompletionRate(songId)
        peak = self.database.getSongPeak(songId) or {}
        peakDay = peak.get("peakDay") or {}
        peakWeek = peak.get("peakWeek") or {}
        timeline = self.database.getSongTimeline(songId)
        timelineChart = self._buildTimelineChart(timeline)

        album = entity.get("album") or {}
        albumId = str(album.get("id") or "")
        albumUrl = f"/album/{albumId}" if albumId and albumId != "0" else None
        entity["albumUrl"] = albumUrl
        entity["completionRate"] = completionRate
        entity["completionRateText"] = f"{completionRate}%" if completionRate is not None else None

        return {
            "entityType": "song",
            "entity": entity,
            "stats": {
                "plays": plays,
                "totalTimeMs": totalTimeMs,
                "totalTimeText": msToString(totalTimeMs),
                "firstListenedText": entity["firstListenedText"],
                "lastListenedText": entity["lastListenedText"],
                "completionRate": completionRate,
                "completionRateText": f"{completionRate}%" if completionRate is not None else None,
                "peakDayText": self._formatDayKey(peakDay.get("date")),
                "peakDayPlays": peakDay.get("plays", 0),
                "peakWeekText": self._formatWeekKey(peakWeek.get("week")),
                "peakWeekPlays": peakWeek.get("plays", 0),
                "uniqueContexts": len({item.get("playedFrom") for item in historyItems if item.get("playedFrom")}),
            },
            "albumUrl": albumUrl,
            "albumName": album.get("name", ""),
            "timeline": timeline,
            "timelineChart": timelineChart,
            "historyItems": historyItems[:100],
            "relatedTracks": historyItems[:100],
            "pageTitle": f"{entity['name']} | Spotify Tracker",
        }

    def _buildArtistDetailContext(self, artistId: str):
        tracks = self.database._loadTracks() or {}
        entries = self.database._loadEntries() or []
        matchingEntries = []
        trackStats = {}
        albumStats = {}
        artistEntity = None
        libraryTotalMs = 0

        for entry in entries:
            libraryTotalMs += entry.get("timePlayed", 0)
            track = tracks.get(entry.get("id"))
            if not track:
                continue
            artist = next((item for item in track.get("artists", []) if str(item.get("id", "")) == str(artistId)), None)
            if not artist:
                continue
            if artistEntity is None:
                artistEntity = dict(artist)
                artistEntity["imageId"] = artist.get("imageId") or track.get("imageId")

            historyItem = dict(track)
            historyItem["playedAt"] = entry.get("playedAt")
            historyItem["timePlayed"] = entry.get("timePlayed", 0)
            historyItem["playedFrom"] = entry.get("playedFrom")
            historyItem["artistEntity"] = artist
            embeddedHistoryItem = self._embedSongTextElements(historyItem)
            matchingEntries.append(embeddedHistoryItem)

            trackId = str(track.get("id") or "")
            trackStat = trackStats.get(trackId)
            if trackStat is None:
                trackStat = dict(embeddedHistoryItem)
                trackStat.pop("playedAt", None)
                trackStat.pop("playedAtText", None)
                trackStat.pop("timePlayed", None)
                trackStat.pop("timePlayedText", None)
                trackStat["plays"] = 0
                trackStat["totalTimeListened"] = 0
                trackStat["firstListenedAt"] = entry.get("playedAt")
                trackStats[trackId] = trackStat
            trackStat["plays"] += 1
            trackStat["totalTimeListened"] += entry.get("timePlayed", 0)

            album = track.get("album") or {}
            albumId = str(album.get("id") or "")
            if albumId and albumId != "0":
                albumStat = albumStats.get(albumId)
                if albumStat is None:
                    albumStat = {
                        "id": albumId,
                        "name": album.get("name", "Unknown album"),
                        "url": album.get("url", ""),
                        "imageId": album.get("imageId") or track.get("imageId"),
                        "totalTracks": album.get("totalTracks", 0),
                        "artist": artist.get("name", ""),
                        "plays": 0,
                        "totalTimeListened": 0,
                        "firstListenedAt": entry.get("playedAt"),
                        "uniqueTracks": set(),
                    }
                    albumStats[albumId] = albumStat
                albumStat["plays"] += 1
                albumStat["totalTimeListened"] += entry.get("timePlayed", 0)
                albumStat["uniqueTracks"].add(trackId)

        if not artistEntity:
            return None

        matchingEntries.sort(key=lambda item: item.get("playedAt", 0), reverse=True)
        totalTimeMs = sum(item.get("timePlayed", 0) for item in matchingEntries)
        plays = len(matchingEntries)
        firstPlayedAt = min((item.get("playedAt", 0) for item in matchingEntries), default=0)
        lastPlayedAt = max((item.get("playedAt", 0) for item in matchingEntries), default=0)

        relatedTracks = sorted(
            trackStats.values(),
            key=lambda item: (item.get("totalTimeListened", 0), item.get("plays", 0)),
            reverse=True,
        )
        relatedTracks = self._embedTopSongsTextElements(
            relatedTracks, sortBy="totalTimeListened", totalPlays=plays, totalMs=totalTimeMs
        )

        albums = []
        for albumStat in albumStats.values():
            uniqueCount = len(albumStat.pop("uniqueTracks"))
            totalTracks = albumStat.get("totalTracks") or 0
            albumStat["uniqueTrackCount"] = uniqueCount
            albumStat["albumDepth"] = round(uniqueCount / totalTracks * 100, 1) if totalTracks else None
            albums.append(albumStat)
        albums.sort(key=lambda item: (item.get("totalTimeListened", 0), item.get("plays", 0)), reverse=True)
        albums = self._embedAlbumsTextElements(
            albums, sortBy="totalTimeListened", totalPlays=plays, totalMs=totalTimeMs
        )

        listenShare = round(totalTimeMs / libraryTotalMs * 100, 1) if libraryTotalMs else 0

        artistEntity["spotifyEmbedUrl"] = self._getSpotifyEmbedUrl("artist", artistEntity.get("url"))
        artistEntity["playCount"] = plays
        artistEntity["totalTimeMs"] = totalTimeMs
        artistEntity["firstListenedText"] = convertToDatetime(firstPlayedAt).strftime("%b %d, %Y") if firstPlayedAt else "Never"
        artistEntity["lastListenedText"] = convertToDatetime(lastPlayedAt).strftime("%b %d, %Y") if lastPlayedAt else "Never"
        artistEntity["listenShare"] = listenShare
        artistEntity["listenShareText"] = f"{listenShare}%"

        return {
            "entityType": "artist",
            "entity": artistEntity,
            "stats": {
                "plays": plays,
                "totalTimeMs": totalTimeMs,
                "totalTimeText": msToString(totalTimeMs),
                "uniqueSongs": len(trackStats),
                "uniqueAlbums": len(albums),
                "listenShare": listenShare,
                "listenShareText": f"{listenShare}%",
                "firstListenedText": artistEntity["firstListenedText"],
                "lastListenedText": artistEntity["lastListenedText"],
            },
            "albums": albums[:100],
            "historyItems": matchingEntries[:100],
            "relatedTracks": relatedTracks[:100],
            "pageTitle": f"{artistEntity['name']} | Spotify Tracker",
        }

    def _rankPlayedTracks(self, historyItems, plays, totalTimeMs) -> list[dict]:
        """Collapse repeated plays into per-track stats sorted by listen time."""
        ranked = {}
        for item in historyItems:
            trackId = str(item.get("id") or "")
            stat = ranked.get(trackId)
            if stat is None:
                stat = dict(item)
                stat.pop("playedAt", None)
                stat.pop("playedAtText", None)
                stat.pop("timePlayed", None)
                stat.pop("timePlayedText", None)
                stat["plays"] = 0
                stat["totalTimeListened"] = 0
                stat["firstListenedAt"] = item.get("playedAt")
                ranked[trackId] = stat
            stat["plays"] += 1
            stat["totalTimeListened"] += item.get("timePlayed", 0)
            if (item.get("playedAt") or 0) < (stat.get("firstListenedAt") or 0):
                stat["firstListenedAt"] = item.get("playedAt")

        rankedTracks = sorted(
            ranked.values(),
            key=lambda item: (item.get("totalTimeListened", 0), item.get("plays", 0)),
            reverse=True,
        )
        return self._embedTopSongsTextElements(
            rankedTracks, sortBy="totalTimeListened", totalPlays=plays, totalMs=totalTimeMs
        )

    def _buildAlbumDetailContext(self, albumId: str):
        tracks = self.database._loadTracks() or {}
        albumMeta = None
        albumTrackIds = set()
        for trackId, track in tracks.items():
            album = track.get("album") or {}
            if not isinstance(album, dict) or str(album.get("id") or "") != str(albumId):
                continue
            albumTrackIds.add(trackId)
            if albumMeta is None:
                albumMeta = album

        if not albumTrackIds or albumMeta is None:
            return None

        historyItems = []
        albumEntries = []
        artistsText = ""
        for entry in (self.database._loadEntries() or []):
            if entry.get("id") not in albumTrackIds:
                continue
            albumEntries.append(entry)
            track = tracks.get(entry.get("id"))
            if not track:
                continue
            historyItem = dict(track)
            historyItem["playedAt"] = entry.get("playedAt")
            historyItem["timePlayed"] = entry.get("timePlayed", 0)
            historyItem["playedFrom"] = entry.get("playedFrom")
            embedded = self._embedSongTextElements(historyItem)
            if not artistsText:
                artistsText = embedded.get("artistsText", "")
            historyItems.append(embedded)

        historyItems.sort(key=lambda item: item.get("playedAt", 0), reverse=True)
        totalTimeMs = sum(item.get("timePlayed", 0) for item in historyItems)
        plays = len(historyItems)
        firstPlayedAt = min((item.get("playedAt", 0) for item in historyItems), default=0)
        lastPlayedAt = max((item.get("playedAt", 0) for item in historyItems), default=0)

        relatedTracks = self._rankPlayedTracks(historyItems, plays, totalTimeMs)
        uniqueTrackCount = len(relatedTracks)
        totalTracks = albumMeta.get("totalTracks") or 0
        albumDepth = round(uniqueTrackCount / totalTracks * 100, 1) if totalTracks else None
        completionRate = self.database.getCompletionRate(entries=albumEntries) if albumEntries else None

        releaseDate = albumMeta.get("releaseDate")
        entity = {
            "name": albumMeta.get("name", "Unknown album"),
            "id": str(albumId),
            "url": albumMeta.get("url") or f"https://open.spotify.com/album/{albumId}",
            "spotifyEmbedUrl": self._getSpotifyEmbedUrl("album", albumMeta.get("url") or str(albumId)),
            "imageId": albumMeta.get("imageId") or (historyItems[0].get("imageId") if historyItems else None),
            "artistsText": artistsText,
            "artist": artistsText,
            "totalTracks": totalTracks,
            "releaseDateText": dateToString(releaseDate) if releaseDate else "",
            "playCount": plays,
            "totalTimeMs": totalTimeMs,
            "firstListenedText": convertToDatetime(firstPlayedAt).strftime("%b %d, %Y") if firstPlayedAt else "Never",
            "lastListenedText": convertToDatetime(lastPlayedAt).strftime("%b %d, %Y") if lastPlayedAt else "Never",
        }

        return {
            "entityType": "album",
            "entity": entity,
            "stats": {
                "plays": plays,
                "totalTimeMs": totalTimeMs,
                "totalTimeText": msToString(totalTimeMs),
                "uniqueTracks": uniqueTrackCount,
                "uniqueSongs": uniqueTrackCount,
                "totalTracks": totalTracks,
                "albumDepth": albumDepth,
                "albumDepthText": f"{albumDepth}% of the album" if albumDepth else None,
                "completionRate": completionRate,
                "completionRateText": f"{completionRate}%" if completionRate is not None else None,
                "firstListenedText": entity["firstListenedText"],
                "lastListenedText": entity["lastListenedText"],
            },
            "historyItems": historyItems[:100],
            "relatedTracks": relatedTracks[:100],
            "pageTitle": f"{entity['name']} | Spotify Tracker",
        }

    def _buildPlaylistDetailContext(self, playlistId: str):
        fullUri, bareId = self._normalizeContextId(playlistId)
        entries = [
            entry for entry in (self.database._loadEntries() or [])
            if self._contextMatches(entry.get("playedFrom"), fullUri, bareId)
        ]
        if not entries:
            return None

        tracks = self.database._loadTracks() or {}
        historyItems = []
        for entry in entries:
            track = tracks.get(entry.get("id"))
            if not track:
                continue
            historyItem = dict(track)
            historyItem["playedAt"] = entry.get("playedAt")
            historyItem["timePlayed"] = entry.get("timePlayed", 0)
            historyItem["playedFrom"] = entry.get("playedFrom")
            historyItems.append(self._embedSongTextElements(historyItem))

        historyItems.sort(key=lambda item: item.get("playedAt", 0), reverse=True)
        totalTimeMs = sum(item.get("timePlayed", 0) for item in historyItems)
        plays = len(historyItems)
        firstPlayedAt = min((item.get("playedAt", 0) for item in historyItems), default=0)
        lastPlayedAt = max((item.get("playedAt", 0) for item in historyItems), default=0)
        relatedTracks = self._rankPlayedTracks(historyItems, plays, totalTimeMs)

        entityName = self._safePlaylistName(fullUri) or bareId
        playCounts = {}
        for item in historyItems:
            playCounts[item.get("imageId")] = playCounts.get(item.get("imageId"), 0) + 1
        playCounts.pop(None, None)
        topImageId = max(playCounts, key=playCounts.get) if playCounts else None
        entity = {
            "name": entityName,
            "id": fullUri,
            "url": f"https://open.spotify.com/playlist/{bareId}",
            "spotifyEmbedUrl": self._getSpotifyEmbedUrl("playlist", fullUri),
            "imageId": topImageId,
            "coverUrl": self._contextCoverUrl(fullUri, topImageId),
            "playCount": plays,
            "totalTimeMs": totalTimeMs,
            "firstListenedText": convertToDatetime(firstPlayedAt).strftime("%b %d, %Y") if firstPlayedAt else "Never",
            "lastListenedText": convertToDatetime(lastPlayedAt).strftime("%b %d, %Y") if lastPlayedAt else "Never",
        }

        return {
            "entityType": "playlist",
            "entity": entity,
            "stats": {
                "plays": plays,
                "totalTimeMs": totalTimeMs,
                "totalTimeText": msToString(totalTimeMs),
                "uniqueSongs": len(relatedTracks),
                "firstListenedText": entity["firstListenedText"],
                "lastListenedText": entity["lastListenedText"],
            },
            "historyItems": historyItems[:100],
            "relatedTracks": relatedTracks[:100],
            "pageTitle": f"{entityName} | Spotify Tracker",
        }

    def getPage(self, items, page, pageSize=50):
        """ Gets items in page as well as other data including total pages and start index """
        page = max(1, page)
        total = len(items)
        totalPages = max(1, (total + pageSize - 1) // pageSize)
        start = (page - 1) * pageSize
        end = start + pageSize
        return (items[start:end], totalPages, start)

    def _getDateRange(self, interval: str = None, customStart: str = None, customEnd: str = None, default="day"):
            """Get start and end dates based on interval or custom dates.

            Returns a half-open local interval [startDate, endDate).
            """
            nowLocal = now()
            startDate = None

            futureBuffer = timedelta(days=1) 

            endDate = nowLocal + futureBuffer   #< bypass any timezone issues

            if customStart and customEnd:
                try:
                    startLocal = parseDateString(customStart)
                    endLocal = parseDateString(customEnd)
                    if startLocal is None or endLocal is None:
                        raise ValueError("Invalid custom date")

                    startDate = startLocal
                    endDate = endLocal + timedelta(days=1)
                except ValueError:
                    pass
            if interval == "":
                interval = default
            if not startDate:
                if interval == "day":
                    startDate = nowLocal - timedelta(days=1)

                elif interval == "week":
                    startDate = nowLocal - timedelta(weeks=1)

                elif interval == "month":
                    startDate = nowLocal - timedelta(days=30)

                elif interval == "year":
                    startDate = nowLocal - timedelta(days=365)

                elif interval == "5years":
                    startDate = nowLocal - timedelta(days=365*5)
                else:
                    startDate = None
                    endDate = None

            return startDate, endDate

    def _buildSummary(self):
        """ Read-only stats for dashboards. Same ranges and totals as the Overview page; no secrets. """
        dayStart, dayEnd = self._getDateRange("day")
        weekStart, weekEnd = self._getDateRange("week")
        today = self.database.getOverallStats(dayStart, dayEnd)
        week = self.database.getOverallStats(weekStart, weekEnd)
        allTime = self.database.getOverallStats(None, None)

        lastPlayed = None
        entries = self.database._loadEntries()
        if entries:
            latest = max(entries, key=lambda e: convertToDatetime(e["playedAt"]))
            track = self.database._loadTracks().get(latest["id"]) or {}
            lastPlayed = {
                "track": track.get("name"),
                "artist": ", ".join(a.get("name", "") for a in track.get("artists") or []) or None,
                "played_at": convertToDatetime(latest["playedAt"]).isoformat(),
            }

        listener = self.database.listener
        lastPoll = getattr(listener, "lastPoll", None)
        return {
            "plays_today": int(today["totalSongsPlayed"]),
            "listen_seconds_today": int(today["totalDurationMs"] // 1000),
            "plays_week": int(week["totalSongsPlayed"]),
            "total_plays": int(allTime["totalSongsPlayed"]),
            "current_streak_days": int(self.database.getStreakStats().get("currentStreak", 0)),
            "last_played": lastPlayed,
            "listener": {
                "connected": bool(self.spotify.isConnected()),
                "last_poll": lastPoll.isoformat() if lastPoll else None,
            },
        }

    def _getIntervalLabel(self, interval: str = None, customStart: str = None, customEnd: str = None):
        labels = {
            "all time": "All Time",
            "day": "Last Day",
            "week": "Last Week",
            "month": "Last Month",
            "year": "Last Year",
            "5years": "Last 5 Years",
        }

        if interval == "custom" and customStart and customEnd:
            return f"Custom range: {customStart} to {customEnd}"

        return labels.get(interval or "day", "Last Day")

    def registerRoutes(self):
        def _is_version_newer(remote: str, local: str) -> bool:
            try:
                return versionTuple(remote) > versionTuple(local)
            except Exception:
                return False

        @self.app.route('/img/<username>/tracks/<filename>')
        def serveTrackImage(username, filename):
            imageDir = os.path.join(self.baseDir, "Database", "Users", username, "img", "tracks")
            return send_from_directory(imageDir, filename)

        @self.app.route('/img/<username>/playlists/<filename>')
        def servePlaylistImage(username, filename):
            imageDir = os.path.join(self.baseDir, "Database", "Users", username, "img", "playlists")
            return send_from_directory(imageDir, filename)

        @self.app.route('/img/<username>/artists/<filename>')
        def serveArtistImage(username, filename):
            imageDir = os.path.join(self.baseDir, "Database", "Users", username, "img", "artists")
            return send_from_directory(imageDir, filename)

        @self.app.route("/import-history", methods=["POST"])
        def importHistory():
            if self.database.readProgress().get("status") == "running":
                return redirect(url_for("importPage"))

            uploads = [f for f in request.files.getlist("history_file") if f and f.filename]
            if not uploads:
                return redirect(url_for("importPage"))

            payloads = [upload.read().decode("utf-8") for upload in uploads]
            thread = threading.Thread(target=self.database.importHistory, args=(payloads,), daemon=True)
            thread.start()
            time.sleep(1)  # Give thread time to start and update progress
            return redirect(url_for("importPage"))

        @self.app.route("/import", methods=["GET"])
        def importPage():
            return render_template("import.html", importProgress=self.database.readProgress())

        def _renderLogin(error=None):
            return render_template(
                "login.html",
                configured=self.spotify.isConfigured(),
                needsRelog=self.spotify.needsRelog(),
                redirectUri=self.spotify.redirectUri,
                nextPath=self._safeNextPath(request.args.get("next")) or "",
                error=error,
            )

        @self.app.route("/login", methods=["GET"])
        def login():
            return _renderLogin()

        @self.app.route("/login/start", methods=["POST"])
        def loginStart():
            if not self.spotify.isConfigured():
                return _renderLogin("Spotify isn't configured. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI.")
            state = self._newOAuthState(self._safeNextPath(request.form.get("next")))
            return redirect(self.spotify.getAuthorizeUrl(state))

        def oauthCallback():
            error = request.args.get("error")
            state = request.args.get("state", "")
            pending = self.pendingOAuthStates.pop(state, None)
            if error:
                return _renderLogin(f"Spotify login was cancelled ({error}).")
            if pending is None:
                return _renderLogin("Login link expired or didn't come from this tracker. Please try again.")
            code = request.args.get("code")
            if not code:
                return _renderLogin("Spotify didn't return a login code. Please try again.")
            try:
                self.spotify.handleCallback(code)
            except (SpotifyOauthError, requests.RequestException) as e:
                print(f"Spotify token exchange failed: {e}")
                return _renderLogin("Couldn't finish the Spotify login. Check the client ID/secret and redirect URI, then try again.")
            self.startListenerIfNeeded()
            return redirect(pending[1] or url_for("dashboard"))

        callbackPath = urlparse(self.spotify.redirectUri).path or "/callback"
        self.app.add_url_rule(callbackPath, "oauthCallback", oauthCallback, methods=["GET"])

        @self.app.route("/spotify-status", methods=["GET"])
        def spotifyStatus():
            return jsonify(self.spotify.status())

        @self.app.route("/api/summary", methods=["GET"])
        def apiSummary():
            return jsonify(self._buildSummary())

        @self.app.route("/data-version", methods=["GET"])
        def dataVersion():
            return jsonify({"version": getattr(self.database, "dataVersion", None)})

        @self.app.context_processor
        def injectDataVersion():
            # Pages that show plays refresh themselves when new plays are saved (not import/login pages)
            liveRefresh = request.endpoint not in ("importPage", "login", "loginStart", "oauthCallback")
            return {"dataVersion": getattr(self.database, "dataVersion", None) if liveRefresh else None}

        @self.app.route("/import-progress", methods=["GET"])
        def importProgress():
            return jsonify(self.database.readProgress())

        @self.app.route("/version_status", methods=["GET"])
        def version_status():
            # Return the current and latest versions (latest is null if not newer)
            with self._version_lock:
                latest = self.latestVersion
            if latest and _is_version_newer(latest, self.currentVersion):
                return jsonify({"current": self.currentVersion, "latest": latest})
            else:
                return jsonify({"current": self.currentVersion, "latest": None})

        @self.app.route("/", methods=["GET"])
        def dashboard():
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            interval = request.args.get("interval", "day")
            if interval == "custom" and not (customStart and customEnd):
                interval = "all time"

            intervalLabel = self._getIntervalLabel(interval, customStart, customEnd)
            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="day")
            stats = self.database.getOverallStats(startDate, endDate)

            chartEndDate = endDate or now()
            heatmapSeries = self._embedActivityPeriodTextElements(
                self.database.getIntervalHeatmap(None, None, every=24),
                every=24,
            )

            barEvery, barSeriesLabel = self._getActivityBarGrouping(interval, startDate, endDate)
            barStartDate = startDate
            barEndDate = min(chartEndDate, now()) if chartEndDate else now()
            barSeries = self._embedActivityPeriodTextElements(
                self.database.getIntervalHeatmap(barStartDate, barEndDate, every=barEvery),
                every=barEvery,
            )

            heatmapYears = self.database.getAvailableYears() or [chartEndDate.year]
            selectedHeatmapYear = chartEndDate.year
            if selectedHeatmapYear not in heatmapYears:
                selectedHeatmapYear = heatmapYears[-1]

            totalDurationText = msToString(stats["totalDurationMs"])

            peekTopSongs = self._embedTopSongsTextElements(
                self._embedSongsTextElements(stats["currentTopSongs"][:5]),
                sortBy="plays",
                totalPlays=stats["totalSongsPlayed"],
                totalMs=stats["totalDurationMs"],
            )
            peekTopArtists = self._embedArtistsTextElements(
                stats["currentTopArtists"][:5],
                sortBy="totalTimeListened",
                totalPlays=stats["totalSongsPlayed"],
                totalMs=stats["totalDurationMs"],
            )
            currentTopSong = peekTopSongs[0] if peekTopSongs else None
            currentTopArtist = peekTopArtists[0] if peekTopArtists else None

            sessionStats = self.database.getSessionStats(startDate, endDate)
            streakStats = self.database.getStreakStats(startDate, endDate)
            avgDailyListenMs = analyticsLib.avgDailyListenMs(stats["totalDurationMs"], sessionStats.get("activeDays", 0))

            totalSongsChangeText, totalSongsChangeClass = self._getChangeText(stats["totalSongsPlayed"], stats["previousSongsPlayed"])
            totalListenChangeText, totalListenChangeClass = self._getChangeText(stats["totalDurationMs"], stats["previousDurationMs"])

            return render_template(
                "overview.html",
                tracks=[],
                totalSongsPlayed=stats["totalSongsPlayed"],
                totalListenTime=totalDurationText,
                totalSongsChangeText=totalSongsChangeText,
                totalSongsChangeClass=totalSongsChangeClass,
                totalListenChangeText=totalListenChangeText,
                totalListenChangeClass=totalListenChangeClass,
                currentTopSong=currentTopSong,
                currentTopArtist=currentTopArtist,
                peekTopSongs=peekTopSongs,
                peekTopArtists=peekTopArtists,
                streakStats=streakStats,
                sessionStats=sessionStats,
                avgDailyListenMs=avgDailyListenMs,
                avgDailyListenTime=msToString(avgDailyListenMs),
                intervalLabel=intervalLabel,
                heatmapSeries=heatmapSeries,
                heatmapYears=heatmapYears,
                selectedHeatmapYear=selectedHeatmapYear,
                barSeries=barSeries,
                barSeriesLabel=barSeriesLabel,
                username=self.username,
                page=1,
                totalPages=1,
                prevUrl=None,
                nextUrl=None,
                startIndex=0,
                section="dashboard",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                activeTab="overview",
            )

        @self.app.route("/history", methods=["GET"])
        def historyPage():
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            page = int(request.args.get("page", 1) or 1)
            searchQuery = request.args.get("q", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            interval = request.args.get("interval", "")
            if interval == "custom" and not (customStart and customEnd):
                interval = "all time"

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time")
            intervalLabel = self._getIntervalLabel(interval or "all time", customStart, customEnd)

            entries = self.database.filterByInterval(self.database._loadEntries(), startDate, endDate)
            tracks = self.database._paginateEntries(entries[::-1])   #< newest first
            totalPlays = len(tracks)
            totalMs = self._getTotal(tracks, "timePlayed")
            self._embedIndices(tracks)
            tracks = self._filterBySearch(tracks, searchQuery)
            tracks, totalPages, startIndex = self.getPage(tracks, page)
            tracks = self._embedSongsTextElements(tracks)

            prevUrl, nextUrl = self._getNeighboringUrls(
                "historyPage",
                page,
                totalPages,
                q=searchQuery,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            return render_template(
                "history.html",
                tracks=tracks,
                totalSongsPlayed=totalPlays,
                totalListenTime=msToString(totalMs),
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                totalSongsChangeText=None,
                totalSongsChangeClass=None,
                totalListenChangeText=None,
                totalListenChangeClass=None,
                currentTopSong=None,
                currentTopArtist=None,
                intervalLabel=intervalLabel,
                heatmapSeries=None,
                heatmapYears=None,
                selectedHeatmapYear=None,
                barSeries=None,
                barSeriesLabel=None,
                username=self.username,
                page=page,
                totalPages=totalPages,
                prevUrl=prevUrl,
                nextUrl=nextUrl,
                startIndex=startIndex,
                section="history",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                activeTab="history",
            )

        @self.app.route("/charts", methods=["GET"])
        def chartsPage():
            return redirect(url_for("dashboard", **request.args))

        @self.app.route("/explore", methods=["GET"])
        def explorePage():
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            tab = request.args.get("tab", "songs")
            tabDefinition = next((item for item in EXPLORE_TABS if item["key"] == tab), EXPLORE_TABS[0])
            tab = tabDefinition["key"]
            section = tabDefinition["section"]

            page = int(request.args.get("page", 1) or 1)
            searchQuery = request.args.get("q", "")
            sortBy = request.args.get("sortBy", "totalTimeListened")
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            if interval == "custom" and not (customStart and customEnd):
                interval = "all time"

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time")
            intervalLabel = self._getIntervalLabel(interval or "all time", customStart, customEnd)

            if tab == "artists":
                rawItems = self.database.getTopArtists(startDate=startDate, endDate=endDate, by=sortBy) or []
            elif tab == "albums":
                rawItems = self.database.getTopAlbums(startDate=startDate, endDate=endDate, by=sortBy) or []
            elif tab == "contexts":
                rawItems = self.database.getTopContexts(startDate=startDate, endDate=endDate, by=sortBy) or []
            else:
                rawItems = self.database.getTopSongs(startDate=startDate, endDate=endDate, by=sortBy) or []

            totalPlays = self._getTotal(rawItems, "plays")
            totalMs = self._getTotal(rawItems, "totalTimeListened")
            if tab == "songs":
                totalUnique = len(rawItems)
            elif tab == "albums":
                totalUnique = self._getTotal(rawItems, "uniqueTrackCount")
            else:
                totalUnique = self._getTotal(rawItems, "uniqueSongCount")

            if searchQuery:
                self._embedIndices(rawItems)
            items = self._filterBySearch(rawItems, searchQuery)
            items, totalPages, startIndex = self.getPage(items, page)

            if tab == "artists":
                items = self._embedArtistsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
            elif tab == "albums":
                items = self._embedAlbumsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
            elif tab == "contexts":
                items = self._embedContextsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
            else:
                items = self._embedSongsTextElements(items)
                items = self._embedTopSongsTextElements(items, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

            prevUrl, nextUrl = self._getNeighboringUrls(
                "explorePage",
                page,
                totalPages,
                tab=tab,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            tabs = [
                {
                    "key": item["key"],
                    "label": item["label"],
                    "active": item["key"] == tab,
                    "url": self._buildPageUrl(
                        "explorePage",
                        1,
                        tab=item["key"],
                        q=searchQuery,
                        sortBy=sortBy,
                        interval=interval,
                        startDate=customStart,
                        endDate=customEnd,
                    ),
                }
                for item in EXPLORE_TABS
            ]

            return render_template(
                "explore.html",
                tracks=items,
                items=items,
                username=self.username,
                tab=tab,
                tabs=tabs,
                tabLabel=tabDefinition["label"],
                totalItems=len(rawItems),
                totalPlays=totalPlays,
                totalUnique=totalUnique,
                totalTime=msToString(totalMs),
                page=page,
                totalPages=totalPages,
                prevUrl=prevUrl,
                nextUrl=nextUrl,
                startIndex=startIndex,
                section=section,
                sortBy=sortBy,
                interval=interval,
                intervalLabel=intervalLabel,
                customStart=customStart,
                customEnd=customEnd,
                activeTab="explore",
            )

        @self.app.route("/top-songs", methods=["GET"])
        def topSongsPage():
            queryArgs = {key: value for key, value in request.args.items() if key != "tab"}
            return redirect(url_for("explorePage", tab="songs", **queryArgs))

        @self.app.route("/top-artists", methods=["GET"])
        def topArtistsPage():
            queryArgs = {key: value for key, value in request.args.items() if key != "tab"}
            return redirect(url_for("explorePage", tab="artists", **queryArgs))

        @self.app.route("/insights", methods=["GET"])
        def insightsPage():
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            interval = request.args.get("interval", "")
            if interval == "custom" and not (customStart and customEnd):
                interval = "all time"

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time")
            intervalLabel = self._getIntervalLabel(interval or "all time", customStart, customEnd)

            try:
                minPlays = max(1, int(request.args.get("minPlays", 10)))
            except (TypeError, ValueError):
                minPlays = 10

            # One pass over entries/tracks for completion avgs (also powers client-side threshold filtering).
            completion = self.database.getCompletionLists(startDate, endDate, minPlays=minPlays, limit=100)
            oftenAbandoned = completion["oftenAbandoned"]
            mostCompleted = completion["mostCompleted"]
            completionUniverse = completion["universe"]

            streakStats = self.database.getStreakStats(startDate, endDate)
            sessionStats = self.database.getSessionStats(startDate, endDate)
            timePatterns = self.database.getTimePatterns(startDate, endDate)
            monthlyStory = self.database.getMonthlyStory(startDate, endDate)
            peakObsession = self.database.getPeakObsession(startDate, endDate, limit=100)
            trendingSongs = self.database.getTrendingSongs(startDate, endDate, limit=100)
            skipStats = self.database.getSkipStats(startDate, endDate, limit=100)
            coListening = self.database.getCoListening(startDate, endDate, limit=100)
            discoveryDays = self.database.getDiscoveryDays(startDate, endDate, limit=100)
            longestSessions = self.database.getLongestSessions(startDate, endDate, limit=100)
            heaviestDays = self.database.getHeaviestDays(startDate, endDate, limit=100)

            rangeEntries = self.database.filterByInterval(self.database._loadEntries(), startDate, endDate)
            totalPlays = len(rangeEntries)
            totalMs = sum(entry.get("timePlayed", 0) for entry in rangeEntries)
            songMs = {}
            artistMs = {}
            tracks = self.database._loadTracks() or {}
            for entry in rangeEntries:
                tid = entry.get("id")
                ms = entry.get("timePlayed", 0)
                if tid:
                    songMs[tid] = songMs.get(tid, 0) + ms
                track = tracks.get(tid) or {}
                for artist in track.get("artists") or []:
                    if isinstance(artist, dict) and artist.get("name"):
                        name = artist["name"]
                        artistMs[name] = artistMs.get(name, 0) + ms
            topSongMs = sorted(songMs.values(), reverse=True)[:10]
            topArtistMs = sorted(artistMs.values(), reverse=True)[:10]
            avgDailyListenMs = analyticsLib.avgDailyListenMs(totalMs, sessionStats.get("activeDays", 0))

            summary = {
                "totalPlays": totalPlays,
                "totalMs": totalMs,
                "totalTimeText": msToString(totalMs),
                "uniqueSongs": len(songMs),
                "uniqueArtists": len(artistMs),
                "avgDailyListenMs": avgDailyListenMs,
                "avgDailyListenTime": msToString(avgDailyListenMs),
                "songConcentration": round(sum(topSongMs) / totalMs * 100, 1) if totalMs else 0,
                "artistConcentration": round(sum(topArtistMs) / totalMs * 100, 1) if totalMs else 0,
            }

            return render_template(
                "insights.html",
                username=self.username,
                streakStats=streakStats,
                sessionStats=sessionStats,
                timePatterns=timePatterns,
                monthlyStory=monthlyStory,
                peakObsession=peakObsession,
                trendingSongs=trendingSongs,
                oftenAbandoned=oftenAbandoned,
                mostCompleted=mostCompleted,
                completionUniverse=completionUniverse,
                minPlays=minPlays,
                skipStats=skipStats,
                coListening=coListening,
                discoveryDays=discoveryDays,
                longestSessions=longestSessions,
                heaviestDays=heaviestDays,
                summary=summary,
                charts=self._buildInsightCharts(timePatterns, monthlyStory),
                section="insights",
                interval=interval,
                intervalLabel=intervalLabel,
                customStart=customStart,
                customEnd=customEnd,
                activeTab="insights",
            )

        @self.app.route("/song/<song_id>", methods=["GET"], endpoint="songDetailPage")
        def songDetailPage(song_id):
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            context = self._buildSongDetailContext(song_id)
            if not context:
                return redirect(url_for("dashboard"))

            return render_template("entity_detail.html", username=self.username, **context)

        @self.app.route("/artist/<artist_id>", methods=["GET"], endpoint="artistDetailPage")
        def artistDetailPage(artist_id):
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            context = self._buildArtistDetailContext(artist_id)
            if not context:
                return redirect(url_for("dashboard"))

            return render_template("entity_detail.html", username=self.username, **context)

        @self.app.route("/album/<album_id>", methods=["GET"], endpoint="albumDetailPage")
        def albumDetailPage(album_id):
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            context = self._buildAlbumDetailContext(album_id)
            if not context:
                return redirect(url_for("dashboard"))

            return render_template("entity_detail.html", username=self.username, **context)

        @self.app.route("/playlist/<path:playlist_id>", methods=["GET"], endpoint="playlistDetailPage")
        def playlistDetailPage(playlist_id):
            if not self.isLoggedIn:
                return redirect(url_for("login", next=request.path))

            context = self._buildPlaylistDetailContext(playlist_id)
            if not context:
                return redirect(url_for("dashboard"))

            return render_template("entity_detail.html", username=self.username, **context)

    def run(self):
        debug = os.environ.get("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")   #< off unless explicitly enabled
        self.app.run(host="0.0.0.0", debug=debug, port=5462, use_reloader=False)#, threaded=False)

if __name__ == "__main__":
    ## $env:IMPORT_KEYWORD="Weekly"
    ## $env:TZ="America/Los_Angeles"

    dashboardApp = SpotifyDashboardApp()
    dashboardApp.run()