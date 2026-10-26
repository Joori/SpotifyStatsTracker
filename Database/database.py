import datetime
import copy
import os
import threading
import json
import time
from pathlib import Path
from io import BytesIO

import requests
from PIL import Image

try:
    from Database.Formatters.spotifyClient import Client, PLACEHOLDER_ARTIST_IMAGE
    from Database.Importers.StreamingHistoryImporter import Importer
    from Database.Importers.AutoImporter import AutoImporter
    from Database.Listeners.spotifyListener import Listener
    from Database.spotifyApi import RateLimitedError, SpotifyAuthError
    from Database.utils import parseError, convertToDatetime, dateToString, now, timeToInt
    from Database import analytics as analyticsLib
    from Database import playMatching
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client, PLACEHOLDER_ARTIST_IMAGE
    from Importers.StreamingHistoryImporter import Importer
    from Importers.AutoImporter import AutoImporter
    from Listeners.spotifyListener import Listener
    from spotifyApi import RateLimitedError, SpotifyAuthError
    from utils import parseError, convertToDatetime, dateToString, now, timeToInt
    import analytics as analyticsLib
    import playMatching

SPOTIFY_MIX_PREFIX = "37i9dQZF"    #< Spotify-made playlists (mixes, Daily Mix, editorial): the Web API returns 404 for these
SPOTIFY_MIX_NAME = "Spotify mix"


class Database:
    def __init__(self,
                 user: str = "Tzur",
                 progressUpdateInterval = 25,   #< Write import progress to disk every N entries instead of every entry
                 spotifyApi = None              #< Database.spotifyApi.SpotifyApi shared by listener, importer and lookups
                 ):
        self.user = user
        self.spotify = spotifyApi
        self.progressUpdateInterval = progressUpdateInterval
        self.listener = None
        self.baseDir = Path(__file__).resolve().parent

        self.imgDir_tracks = self.baseDir / "Users" / self.user / "img" / "tracks"
        self.imgDir_artists = self.baseDir / "Users" / self.user / "img" / "artists"
        self.imgDir_playlists = self.baseDir / "Users" / self.user / "img" / "playlists"   #< covers for playlist_overrides.json
        self.playlistOverridesPath = self.baseDir / "Users" / self.user / "playlist_overrides.json"
        self.entriesPath = self.baseDir / "Users" / self.user / "entries.json"
        self.tracksPath = self.baseDir / "Users" / self.user / "tracks.json"
        self.playlistsPath = self.baseDir / "Users" / self.user / "playlists.json"
        self.progressPath = self.baseDir / "Users" / self.user / "progress.json"
        self.autoImportFolderPath = self.baseDir / ".." / "autoImport"

        self.fileLock = threading.RLock()
        self.entriesCache = None
        self.tracksCache = None
        self.playlistsCache = None
        self._overridesCache = (None, {})   #< (file mtime, overrides), re-read when the file changes
        self._dataVersionBase = time.time_ns()   #< a restart counts as a change, so open pages reload once
        self._dataVersionCount = 0

        filterKeyword = os.environ.get("IMPORT_KEYWORD", None)
        print(f"auto import filtering by {filterKeyword}")
        self.autoImporter = AutoImporter(folderPath=self.autoImportFolderPath,
                                         importCallback=self.importHistory,
                                         pollInterval=5,
                                         keyword=filterKeyword)

    def _loadJsonFile(self, path: Path, default):
        with self.fileLock:
            path.parent.mkdir(parents=True, exist_ok=True)

            if not path.exists():
                path.write_text(
                    json.dumps(default, indent=4),
                    encoding="utf-8"
                )
                return default

            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"Corrupted JSON in {path}: {e}")
                raise

    def _save(self, file, data):
        with self.fileLock:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(data, indent=4), encoding="utf-8")

    def _addToDatabaseFromListener(self, items) -> None:
        """
        Store plays from the official recently-played endpoint.
        The API gives no listen time and played_at is (roughly) when the play ended, so the
        start is estimated as played_at - track length and the play is flagged "estimated".
        """
        if not items:
            return
        candidates = []
        for item in items:
            raw = item.get("track") or {}
            if not raw.get("id") or raw.get("is_local") or raw.get("type", "track") != "track":
                continue
            endTimestamp = timeToInt(item.get("played_at"))
            if not endTimestamp:
                continue
            duration = raw.get("duration_ms") or 0
            try:
                formatted = Client.formatTrack(raw, embedPlaybackInfo=False)
            except Exception as e:
                print(f"Skipping unreadable recently-played item: {parseError(e)}")
                continue
            candidates.append({
                "id": formatted["id"],
                "playedAt": endTimestamp - duration // 1000,
                "duration": duration,
                "key": playMatching.songKey(formatted),
                "raw": raw,
                "context": item.get("context"),
                "endedAt": item.get("played_at"),
            })
        if not candidates:
            return

        windowStart = min(c["playedAt"] for c in candidates) - 6 * 3600
        with self.fileLock:
            recentEntries = [e for e in self._loadEntries() if (e.get("playedAt") or 0) >= windowStart]
            fresh = playMatching.filterNewLivePlays(candidates, recentEntries, self._loadTracks())

        for candidate in sorted(fresh, key=lambda c: c["playedAt"]):
            meta = Client.formatTrack(candidate["raw"], candidate["playedAt"], candidate["duration"], context=candidate["context"])
            meta["estimated"] = True
            artists = ", ".join(a.get("name", "") for a in candidate["raw"].get("artists") or meta.get("artists") or [])
            name = meta.get("name")
            self.appendMetadata(meta)
            print(f"Listener recorded: {name} - {artists} (played_at {candidate['endedAt']})")
        if fresh:
            print(f"Listener recorded {len(fresh)} new play(s), {len(candidates) - len(fresh)} already stored")

    def _loadEntries(self) -> list:
        """Load ONLY id and info about time played from the JSON file."""
        if self.entriesCache is None:
            self.entriesCache = self._loadJsonFile(self.entriesPath, [])
        return self.entriesCache

    def _saveEntries(self, entries: list):
        """Save ONLY id and info about time played to the JSON file."""
        self.entriesCache = entries
        self._save(self.entriesPath, entries)
        self._dataVersionCount += 1

    @property
    def dataVersion(self) -> str:
        """ Changes whenever plays are saved; open pages poll it to know when to refresh. """
        return f"{self._dataVersionBase}-{self._dataVersionCount}"

    def _saveTracks(self, tracks: dict):
        """Save full track metadata to the JSON file."""
        self.tracksCache = tracks
        self._save(self.tracksPath, tracks)

    def _loadTracks(self) -> dict:
        """Load full track metadata from the JSON file."""
        if self.tracksCache is None:
            self.tracksCache = self._loadJsonFile(self.tracksPath, {})
        return self.tracksCache

    def _loadPlaylists(self) -> dict:
        """Load playlist id to name mappings from the JSON file."""
        if self.playlistsCache is None:
            self.playlistsCache = self._loadJsonFile(self.playlistsPath, {"album": {}, "playlist": {}})
        return self.playlistsCache

    def _savePlaylists(self, playlists: dict):
        """Save playlist id to name mappings to the JSON file."""
        self.playlistsCache = playlists
        self._save(self.playlistsPath, playlists)
    
    def _addTrack(self, tracks, track):
        tracks[track["id"]] = track
        return tracks

    def _saveNewTrackFromId(self, id, tracks=None):
        if tracks == None:
            tracks = self._loadTracks()
        if self.spotify is None:
            raise SpotifyAuthError("notConfigured")
        track = Client.formatTrack(self.spotify.track(id), embedPlaybackInfo=False)   #< never waits on a rate limit (may run in a web request)
        tracks = self._addTrack(tracks, track)
        self._saveTracks(tracks)

    def _splitEntryAndTrack(self, metadata: dict) -> tuple[list, dict]:
        entry = {
            "id": metadata["id"],
            "playedAt": metadata["playedAt"],
            "timePlayed": metadata["timePlayed"],
            "playedFrom": metadata.get("playedFrom"),
        }
        if "skipped" in metadata:
            entry["skipped"] = bool(metadata.get("skipped"))
        if metadata.get("estimated"):
            entry["estimated"] = True    #< listen time is the track length, not measured
        metadata.pop("playedAt")
        metadata.pop("timePlayed")
        metadata.pop("playedFrom", None)
        metadata.pop("skipped", None)
        metadata.pop("estimated", None)
        return entry, metadata

    def _paginateEntry(self, entry: dict, tracks: dict = None) -> dict:
        if tracks is None:
            tracks = self._loadTracks()

        if entry["id"] not in tracks:
            print(f"Missing track metadata for {entry['id']}, downloading it")
            try:
                self._saveNewTrackFromId(entry["id"], tracks)
            except Exception:
                print("Failed to download track")
                return None

        meta = tracks[entry["id"]].copy()

        meta["playedAt"] = entry["playedAt"]
        meta["timePlayed"] = entry["timePlayed"]
        meta["playedFrom"] = entry.get("playedFrom", None)
        if "skipped" in entry:
            meta["skipped"] = entry.get("skipped")
        if entry.get("estimated"):
            meta["estimated"] = True

        return meta

    def _paginateEntries(self, entries: list) -> list:
        ret = []
        tracks = self._loadTracks()
        for entry in entries:
            metadata = self._paginateEntry(entry, tracks)
            if metadata != None:
                ret.append(metadata)
        return ret

    def appendEntries(self, newEntries: list):
        if not newEntries:
            return
        with self.fileLock:
            entries = self._loadEntries()
            entries.append(newEntries)
            self._saveEntries(entries)
    
    def updateTracks(self, track: dict):
        if not track:
            return
        existingTracks = self._loadTracks()
        self._addTrack(existingTracks, track)          #< Add new track if missing, or update existing track metadata if already exists
        self._saveTracks(existingTracks)

    def updatePlaylists(self, playlist):
        if playlist is None:
            return
        existingPlaylists = self._loadPlaylists()
        contextType, playlistId = playlist.split(":", 1)
        if playlistId not in existingPlaylists[contextType]:
            if self.spotify is None:
                return
            try:
                if contextType == "album":
                    existingPlaylists[contextType][playlistId] = self.spotify.albumName(playlistId)
                else:
                    existingPlaylists[contextType][playlistId] = self.spotify.playlistName(playlistId)
            except (RateLimitedError, SpotifyAuthError) as e:
                print(f"Skipping name lookup for {playlist} for now: {e}")   #< retried on the next play from it
                return
            except Exception as e:
                # Cached either way, so it isn't looked up again
                if playlistId.startswith(SPOTIFY_MIX_PREFIX):
                    print(f"Name lookup for {playlist} failed ({parseError(e)}); Spotify-made playlist, saved as '{SPOTIFY_MIX_NAME}'")
                    existingPlaylists[contextType][playlistId] = SPOTIFY_MIX_NAME
                else:
                    print(f"Error occurred while fetching playlist name for {playlistId} (probably due to playlist being private): {e}")
                    existingPlaylists[contextType][playlistId] = None
            self._savePlaylists(existingPlaylists)

    def getHistory(self) -> int:
        history = self._loadEntries()
        return self._paginateEntries(history)

    def getEntriesCount(self) -> int:
        """Return total number of entries in the database."""
        return len(self._loadEntries())

    def getAvailableYears(self) -> list[int]:
        entries = self._loadEntries()
        years = sorted({convertToDatetime(entry["playedAt"]).year for entry in entries if entry.get("playedAt") is not None})
        return years
    
    def getEntriesFromNew(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True) -> list:
        """ Return the latest `count` entries from history, sorted from newest to oldest. If count is None, return all entries. """
        entries = self._loadEntries()
        startPos = len(entries) - startIndex   #< Everything is reversed
        
        if count is not None:
            endPos = startPos - count
            endPos = None if endPos <= 0 else endPos
            slicedEntries = entries[startPos - 1 : endPos : -1]   #< slice and reverse
        else:
            slicedEntries = entries[startPos - 1 : : -1]          #< slice and reverse
        
        if fullPagination:
            return self._paginateEntries(slicedEntries)
        return slicedEntries

    def getEntriesFromOld(self, count: int | None = None, startIndex: int = 0, fullPagination: bool = True) -> list:
            """ Return the oldest `count` entries from history, sorted from oldest to newest. If count is None, return all entries. """
            entries = self._loadEntries()

            if count is not None:
                endIndex = startIndex + count
                slicedEntries = entries[startIndex:endIndex]
            else:
                slicedEntries = entries[startIndex:]
                
            if fullPagination:
                return self._paginateEntries(slicedEntries)
            return slicedEntries

    def _loadPlaylistOverrides(self) -> dict:
        """
        User-edited playlist_overrides.json: {"<playlist id>": {"name": "...", "cover": "<file in img/playlists/>"}}.
        Re-read whenever the file changes, so edits apply without a restart.
        """
        try:
            mtime = self.playlistOverridesPath.stat().st_mtime
        except OSError:
            return {}
        if self._overridesCache[0] == mtime:
            return self._overridesCache[1]
        overrides = {}
        try:
            raw = json.loads(self.playlistOverridesPath.read_text(encoding="utf-8"))
            for key, value in (raw or {}).items():
                if isinstance(value, dict):
                    overrides[str(key).split(":")[-1]] = value   #< accepts "id", "playlist:id" or "spotify:playlist:id"
        except (OSError, ValueError, AttributeError) as e:
            print(f"Ignoring {self.playlistOverridesPath.name}: {parseError(e)}")
        self._overridesCache = (mtime, overrides)
        return overrides

    def _playlistOverride(self, playlistUri) -> dict:
        if not playlistUri or ":" not in playlistUri:
            return {}
        contextType, playlistId = playlistUri.split(":", 1)
        if contextType != "playlist":
            return {}
        return self._loadPlaylistOverrides().get(playlistId, {})

    def playlistCoverFile(self, playlistUri: str | None) -> str | None:
        """ Override cover filename, only if that file exists in img/playlists/. """
        cover = self._playlistOverride(playlistUri).get("cover")
        if not isinstance(cover, str) or not cover or Path(cover).name != cover:
            return None
        return cover if (self.imgDir_playlists / cover).is_file() else None

    def playlistName(self, playlistUri: str | None) -> str | None:
        """Return the playlist name for a Spotify playlist URI or id, caching it on first lookup."""
        if not playlistUri:
            return None
        override = self._playlistOverride(playlistUri).get("name")
        if isinstance(override, str) and override.strip():
            return override.strip()
        type, playlistId = playlistUri.split(":", 1)
        playlists = self._loadPlaylists()
        name = playlists.get(type, {}).get(playlistId, None)
        if name is None and type == "playlist" and playlistId.startswith(SPOTIFY_MIX_PREFIX):
            return SPOTIFY_MIX_NAME   #< failed lookups cached before this name existed
        return name

    def writeProgress(self, status: str, current: int = 0, total: int = 0, message: str = "", error: bool = False):
        payload = {
            "status": status,
            "current": current,
            "total": total,
            "percentage": round((current / total * 100) if total else 0),
            "message": message,
            "error": error,
        }
        self.progressPath.parent.mkdir(parents=True, exist_ok=True)
        self.progressPath.write_text(json.dumps(payload, indent=4), encoding="utf-8")

    def readProgress(self) -> dict:
        defaultProgress = {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percentage": 0,
            "message": "",
            "error": False,
        }
        if not self.progressPath.exists():
            return defaultProgress
        try:
            return json.loads(self.progressPath.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return defaultProgress
    
    def resetProgress(self):
        self.writeProgress("idle", 0, 0, "", False)

    def _downloadedImageIds(self, path: Path) -> list:
        path.mkdir(parents=True, exist_ok=True)
        return self._loadJsonFile(path / "metadata.json", [])

    def _saveImg(self, path: Path, url: str, imgId: str, final: bool = True):
        """ final=False saves a stand-in image but leaves the id unrecorded so it's retried later. """
        metadataPath = path / "metadata.json"
        ids = self._downloadedImageIds(path)
        if imgId in ids:
            return
        if not final and any(path.glob(f"{imgId}.*")):
            return
        try:
            response = requests.get(url)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            ext = img.format.lower() if img.format else "jpeg"
            
            img.save(path / f"{imgId}.{ext}")
            if not final:
                return
            ids.append(imgId)

            metadataPath.write_text(
                json.dumps(ids, indent=4), encoding="utf-8"
            )
        except requests.exceptions.RequestException as e:
            print(f"Error fetching image from {url}: {parseError(e)}")
        except Exception as e:
            print(f"Error saving image: {parseError(e)}")

    def saveTrackImg(self, url: str, imgId: str):
        if not url or not imgId:
            return
        self._saveImg(self.imgDir_tracks, url, imgId)

    def saveArtistImg(self, url: str, imgId: str, final: bool = True):
        if not url or not imgId:
            return
        self._saveImg(self.imgDir_artists, url, imgId, final)

    def _lookupArtistImageUrl(self, artistId):
        """ Real artist photo via GET /artists/{id}. '' = artist has none, None = lookup failed (retry later). """
        if self.spotify is None or not artistId or str(artistId).startswith("stub-artist:"):
            return ""
        try:
            images = (self.spotify.artist(artistId) or {}).get("images") or []   #< never waits on a rate limit
        except (RateLimitedError, SpotifyAuthError):
            return None
        except Exception as e:
            print(f"Artist image lookup failed for {artistId}: {parseError(e)}")
            return None
        return images[0].get("url", "") if images else ""

    def saveImagesFromTrack(self, track: dict):
        self.saveTrackImg(track["imageUrl"], track["imageId"])
        downloaded = None
        for artist in track["artists"]:
            url = artist.get("imageUrl")
            imgId = artist.get("imageId")
            final = True
            if imgId and url in (None, "", PLACEHOLDER_ARTIST_IMAGE):
                if downloaded is None:
                    downloaded = set(self._downloadedImageIds(self.imgDir_artists))
                if imgId in downloaded:
                    continue      #< already have this artist's photo, no API call
                realUrl = self._lookupArtistImageUrl(artist.get("id"))
                if realUrl:
                    url = realUrl
                    artist["imageUrl"] = realUrl
                elif realUrl is None:
                    url = PLACEHOLDER_ARTIST_IMAGE
                    final = False
                else:
                    url = PLACEHOLDER_ARTIST_IMAGE
            self.saveArtistImg(url, imgId, final)

    def appendMetadata(self, meta: dict) -> None:
        """ Stores the play. Image and playlist-name lookups are extras: if they fail, the play is still saved. """
        try:
            self.saveImagesFromTrack(meta)
        except Exception as e:
            print(f"Couldn't save images for {meta.get('name')}: {parseError(e)}")
        entry, track = self._splitEntryAndTrack(meta)
        self.updateTracks(track)
        self.appendEntries(entry)
        try:
            self.updatePlaylists(entry.get("playedFrom", None))
        except Exception as e:
            print(f"Couldn't look up the name of {entry.get('playedFrom')}: {parseError(e)}")

    def appendTrackData(self, timestamp, track, timePlayed, context=None):
        self.appendMetadata(Client.formatTrack(track, timestamp, timePlayed, context=context))

    def resortDatabase(self):
        """ In case entries got out of order, this will sort them by playedAt timestamp. """
        entries = self._loadEntries()
        entries.sort(
            key=lambda x: convertToDatetime(x["playedAt"]).timestamp()
        )
        print("Resorted Database")

        self._saveEntries(entries)

    def _findTimelineGaps(self, entries: list, minGapDays: int = 7) -> list:
        """Return gaps between consecutive plays longer than minGapDays."""
        gaps = []
        minGapSeconds = minGapDays * 24 * 3600
        for i in range(1, len(entries)):
            start = convertToDatetime(entries[i - 1]["playedAt"]).timestamp()
            end = convertToDatetime(entries[i]["playedAt"]).timestamp()
            diff = end - start
            if diff >= minGapSeconds:
                gaps.append({
                    "days": round(diff / (24 * 3600), 1),
                    "from": dateToString(start),
                    "to": dateToString(end),
                })
        gaps.sort(key=lambda g: -g["days"])
        return gaps

    def _formatImportSummary(self, saved: int, total: int, gaps: list) -> str:
        skipped = max(total - saved, 0)
        parts = [f"Imported {saved} of {total} plays"]
        if skipped:
            parts.append(f"{skipped} skipped (no track name/artist)")
        if gaps:
            largest = gaps[0]
            parts.append(
                f"warning: {len(gaps)} gap(s) over 7 days "
                f"(largest {largest['days']}d from {largest['from']} to {largest['to']}). "
                f"Upload all StreamingHistory / Streaming_History files from your Spotify export to fill them"
            )
        return ". ".join(parts)

    def importHistory(self, exportedHistory):
        tracks = self._loadTracks()
        importer = Importer(api=self.spotify)

        if isinstance(exportedHistory, str):
            exportedHistory = [exportedHistory]

        parsedHistory = []
        exportType = "None"
        skippedFiles = 0
        for payload in exportedHistory:
            parsed, payloadType = importer._convertToList(payload)
            if not parsed:
                skippedFiles += 1
                continue
            if exportType == "None":
                exportType = payloadType
            elif payloadType != exportType:
                print(f"Skipping file with mismatched export type '{payloadType}' (expected '{exportType}')")
                skippedFiles += 1
                continue
            parsedHistory.extend(parsed)

        if not parsedHistory:
            self.writeProgress(
                "failed",
                0,
                0,
                "No valid streaming history found in uploaded files",
                error=True,
            )
            return

        total = len(parsedHistory)
        self.writeProgress("running", 0, total, "Starting import")

        # Plays are only written once the whole import succeeds; track metadata is kept as it's fetched
        newEntries = []
        saved = 0
        try:
            for saved, meta in enumerate(importer.importHistory(parsedHistory, list(tracks.values()), exportType, progressCallback=self.writeProgress), start=1):
                e, t = self._splitEntryAndTrack(meta)
                newEntries.append(e)
                with self.fileLock:
                    tracks = self._addTrack(tracks, t)
                self.saveImagesFromTrack(t)

                if saved % self.progressUpdateInterval == 0 or saved == total:
                    self.writeProgress("running", saved, total, f"Imported {saved} of {total}")
        except Exception as e:
            with self.fileLock:
                self._saveTracks(tracks)
            if isinstance(e, RateLimitedError):
                message = (f"{e}. No plays were saved; track info fetched so far was kept, "
                           f"so running the import again later will be faster")
            elif isinstance(e, SpotifyAuthError):
                message = "Spotify login needed. Relog to Spotify and run the import again"
            else:
                message = f"{parseError(e)}. No plays were saved"
            self.writeProgress("failed", saved, total, message, error=True)
            raise

        with self.fileLock:
            entries = self._loadEntries()
            replaced, toAdd, duplicates = playMatching.mergeImported(entries, newEntries, tracks)
            replacedIds = {id(e) for e in replaced}
            merged = [e for e in entries if id(e) not in replacedIds] + toAdd
            self._saveEntries(merged)
            self._saveTracks(tracks)
            self.resortDatabase()
            gaps = self._findTimelineGaps(self._loadEntries())

        message = self._formatImportSummary(len(toAdd), total, gaps)
        if replaced:
            message += f". {len(replaced)} live-recorded play(s) replaced with exact data from the export"
        if duplicates:
            message += f". {duplicates} play(s) were already in the database and skipped"
        if skippedFiles:
            message += f". {skippedFiles} uploaded file(s) were skipped (unreadable or mismatched type)"
        self.writeProgress("complete", len(toAdd), total, message)

    def filterByInterval(self, entries: list, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        if startDate is None and endDate is None:
            return entries

        filtered = []
        for track in entries:
            playedAt = track["playedAt"]
            date = convertToDatetime(playedAt)

            if startDate and date < startDate:
                continue
            if endDate and date > endDate:
                break
                
            filtered.append(track)
        return filtered

    def getSongsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """Return songs sorted by play count with full song metadata and listen totals."""
        tracks = self._loadTracks()
        entries = self.filterByInterval(self._loadEntries(), startDate, endDate)
        songs = {}

        for entry in entries:
            key = entry["id"]
            timePlayed = entry["timePlayed"]
            playedAt = entry.get("playedAt")
            
            if key not in songs:
                metadata = self._paginateEntry(entry, tracks)  #< Get full song metadata for this entry
                if metadata == None:
                    continue
                songs[key] = metadata
                songs[key].pop("estimated", None)   #< per-play flag, not a property of the song
                songs[key]["plays"] = 0
                songs[key]["totalTimeListened"] = 0
                songs[key]["measuredPlays"] = 0
                songs[key]["measuredTimeListened"] = 0
                songs[key]["firstListenedAt"] = playedAt       #< database is sorted, so first find must be first time listened

            songs[key]["plays"] += 1
            songs[key]["totalTimeListened"] += timePlayed
            if not entry.get("estimated"):   #< completion % only uses plays with a measured listen time
                songs[key]["measuredPlays"] += 1
                songs[key]["measuredTimeListened"] += timePlayed
        return list(songs.values())

    def getArtistsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """Return artists sorted by total plays with aggregated data and listen totals."""
        tracks = self._loadTracks()
        entries = self.filterByInterval(self._loadEntries(), startDate, endDate)
        artistsStats = {}

        for entry in entries:
            timePlayed = entry["timePlayed"]
            playedAt = entry.get("playedAt")
            metadata = self._paginateEntry(entry, tracks)
            if metadata == None:
                continue
            
            artists = metadata.get("artists", [])
            for artist in artists:
                artistName = artist["name"]
                if artistName not in artistsStats:
                    artistsStats[artistName] = artist
                    artistsStats[artistName]["plays"] = 0
                    artistsStats[artistName]["totalTimeListened"] = 0
                    artistsStats[artistName]["uniqueSongs"] = set()
                    artistsStats[artistName]["firstListenedAt"] = playedAt

                artistsStats[artistName]["plays"] += 1
                artistsStats[artistName]["totalTimeListened"] += timePlayed
                artistsStats[artistName]["uniqueSongs"].add(entry.get("id"))

        normalized = []
        for v in artistsStats.values():
            uniqueSongCount = len(v["uniqueSongs"])
            v["uniqueSongCount"] = uniqueSongCount
            v.pop("uniqueSongs")
            normalized.append(v)

        return normalized
    
    def _getListeningTotals(self, entries):
        totalSongsPlayed = 0
        totalDurationMs = 0
        for entry in entries:
            totalSongsPlayed += 1
            totalDurationMs += entry["timePlayed"]
        return totalSongsPlayed, totalDurationMs

    def _getTotal(self, arr, key):
        return sum(i.get(key, 0) for i in arr)

    def getOverallStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        """Return songs sorted by play count with full song metadata and listen totals."""
        previousEntries = []
        if startDate and endDate:
            duration = endDate - startDate
            previousStart = startDate - duration
            previousEnd = startDate
            previousEntries = self.filterByInterval(self._loadEntries(),
                                                    previousStart,
                                                    previousEnd)
        currentTopSongs = self.getTopSongs(startDate=startDate, endDate=endDate, by="plays")
        currentTopArtists = self.getTopArtists(startDate=startDate, endDate=endDate, by="totalTimeListened")
        
        # By using the already calculated currentTopSongs, we can save a lot of time by not having to iterate through the entire entries list again to calculate the totals.
        totalSongsPlayed = self._getTotal(currentTopSongs, "plays")
        totalDurationMs = self._getTotal(currentTopSongs, "totalTimeListened")
        previousSongsPlayed, previousDurationMs = self._getListeningTotals(previousEntries)
        
        return {"currentTopSongs": currentTopSongs,
                "currentTopArtists": currentTopArtists,
                "totalSongsPlayed": totalSongsPlayed,
                "totalDurationMs": totalDurationMs,
                "previousSongsPlayed": previousSongsPlayed,
                "previousDurationMs": previousDurationMs
                }

    def _sortTopStats(self, items, compareKeys, by: str = "plays") -> list:
        """
        Sorts songs within a date range. 
        'plays' and 'totalTimeListened' are sorted descending (highest first).
        'name' is sorted ascending (A to Z).
        """

        reverse=True
        if by == "name":
            reverse=False

        return sorted(
            items, 
            key=lambda item: tuple(item[key] for key in compareKeys),     #< the tuple acts as a tie breaker, if two elements 'by' are the same, it compares the 'totalTimeListened', then 'name'
            reverse=reverse
        )

    def getTopSongs(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays") -> list:
        songs = self.getSongsStats(startDate, endDate)
        compKeys = (by, "totalTimeListened", "name")
        return self._sortTopStats(songs, compKeys, by)

    def getTopArtists(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays") -> list:
        artists = self.getArtistsStats(startDate, endDate)
        compKeys = (by, "totalTimeListened", "name")
        return self._sortTopStats(artists, compKeys, by)

    def getAlbumsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        tracks = self._loadTracks()
        entries = self.filterByInterval(self._loadEntries(), startDate, endDate)
        albums = {}

        for entry in entries:
            metadata = self._paginateEntry(entry, tracks)
            if metadata is None:
                continue
            album = metadata.get("album") or {}
            albumId = str(album.get("id") or "")
            if not albumId or albumId == "0":
                continue
            if albumId not in albums:
                primaryArtist = ""
                artists = metadata.get("artists") or []
                if artists and isinstance(artists[0], dict):
                    primaryArtist = artists[0].get("name", "")
                albums[albumId] = {
                    "id": albumId,
                    "name": album.get("name", "Unknown album"),
                    "url": album.get("url", ""),
                    "imageId": album.get("imageId") or metadata.get("imageId"),
                    "totalTracks": album.get("totalTracks", 0),
                    "artist": primaryArtist,
                    "plays": 0,
                    "totalTimeListened": 0,
                    "uniqueTracks": set(),
                    "firstListenedAt": entry.get("playedAt"),
                }
            albums[albumId]["plays"] += 1
            albums[albumId]["totalTimeListened"] += entry.get("timePlayed", 0)
            albums[albumId]["uniqueTracks"].add(entry.get("id"))

        normalized = []
        for album in albums.values():
            uniqueCount = len(album["uniqueTracks"])
            totalTracks = album.get("totalTracks") or 0
            album["uniqueTrackCount"] = uniqueCount
            album["albumDepth"] = round(uniqueCount / totalTracks * 100, 1) if totalTracks else None
            album.pop("uniqueTracks", None)
            normalized.append(album)
        return normalized

    def getTopAlbums(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays") -> list:
        albums = self.getAlbumsStats(startDate, endDate)
        compKeys = (by, "totalTimeListened", "name")
        return self._sortTopStats(albums, compKeys, by)

    def getContextsStats(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None) -> list:
        entries = self.filterByInterval(self._loadEntries(), startDate, endDate)
        contexts = {}
        for entry in entries:
            playedFrom = entry.get("playedFrom")
            if not playedFrom:
                continue
            if playedFrom not in contexts:
                contextType = playedFrom.split(":", 1)[0] if ":" in playedFrom else "unknown"
                contexts[playedFrom] = {
                    "id": playedFrom,
                    "name": self.playlistName(playedFrom) or playedFrom.split(":", 1)[-1],
                    "contextType": contextType,
                    "plays": 0,
                    "totalTimeListened": 0,
                    "uniqueSongs": {},
                    "firstListenedAt": entry.get("playedAt"),
                }
            contexts[playedFrom]["plays"] += 1
            contexts[playedFrom]["totalTimeListened"] += entry.get("timePlayed", 0)
            songs = contexts[playedFrom]["uniqueSongs"]
            songs[entry.get("id")] = songs.get(entry.get("id"), 0) + 1

        tracks = self._loadTracks()
        normalized = []
        for ctx in contexts.values():
            songs = ctx.pop("uniqueSongs")
            ctx["uniqueSongCount"] = len(songs)
            ctx["coverFile"] = self.playlistCoverFile(ctx["id"])
            topTrackId = max(songs, key=songs.get) if songs else None
            ctx["imageId"] = (tracks.get(topTrackId) or {}).get("imageId")   #< most-played track's cover
            normalized.append(ctx)
        return normalized

    def getTopContexts(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, by: str = "plays") -> list:
        contexts = self.getContextsStats(startDate, endDate)
        for ctx in contexts:
            ctx.setdefault("name", ctx.get("id", ""))
        compKeys = (by, "totalTimeListened", "name")
        return self._sortTopStats(contexts, compKeys, by)

    def getStreakStats(self, startDate=None, endDate=None) -> dict:
        return analyticsLib.streakStats(self._loadEntries(), startDate, endDate, nowDt=now())

    def getSessionStats(self, startDate=None, endDate=None) -> dict:
        return analyticsLib.sessionStats(self._loadEntries(), startDate, endDate)

    def getTimePatterns(self, startDate=None, endDate=None) -> dict:
        return analyticsLib.timePatterns(self._loadEntries(), startDate, endDate)

    def getMonthlyStory(self, startDate=None, endDate=None) -> list:
        return analyticsLib.monthlyStory(self._loadEntries(), self._loadTracks(), startDate, endDate)

    def getPeakObsession(self, startDate=None, endDate=None, limit=25) -> dict:
        return analyticsLib.peakObsession(self._loadEntries(), self._loadTracks(), startDate, endDate, limit)

    def getTrendingSongs(self, startDate=None, endDate=None, limit=10) -> list:
        return analyticsLib.trendingSongs(self._loadEntries(), self._loadTracks(), startDate, endDate, limit)

    def getOftenAbandoned(self, startDate=None, endDate=None, minPlays=5, limit=10) -> list:
        return analyticsLib.oftenAbandoned(self._loadEntries(), self._loadTracks(), startDate, endDate, minPlays, limit)

    def getMostCompleted(self, startDate=None, endDate=None, minPlays=5, limit=10) -> list:
        return analyticsLib.mostCompleted(self._loadEntries(), self._loadTracks(), startDate, endDate, minPlays, limit)

    def getCompletionLists(self, startDate=None, endDate=None, minPlays=5, limit=100) -> dict:
        return analyticsLib.completionLists(self._loadEntries(), self._loadTracks(), startDate, endDate, minPlays, limit)

    def getSkipStats(self, startDate=None, endDate=None, minPlays=5, limit=10) -> dict:
        return analyticsLib.skipStats(self._loadEntries(), self._loadTracks(), startDate, endDate, minPlays, limit)

    def getCoListening(self, startDate=None, endDate=None, limit=15, minCount=3) -> dict:
        return analyticsLib.coListening(self._loadEntries(), self._loadTracks(), startDate, endDate, limit, minCount)

    def getDiscoveryDays(self, startDate=None, endDate=None, limit=10) -> dict:
        return analyticsLib.discoveryDays(self._loadEntries(), self._loadTracks(), startDate, endDate, limit)

    def getHeaviestDay(self, startDate=None, endDate=None) -> dict | None:
        return analyticsLib.heaviestDay(self._loadEntries(), startDate, endDate)

    def getLongestSessions(self, startDate=None, endDate=None, limit=10) -> list:
        return analyticsLib.longestSessions(self._loadEntries(), self._loadTracks(), startDate, endDate, limit)

    def getHeaviestDays(self, startDate=None, endDate=None, limit=10) -> list:
        return analyticsLib.heaviestDays(self._loadEntries(), self._loadTracks(), startDate, endDate, limit)

    def getSongPeak(self, songId: str) -> dict:
        return analyticsLib.songPeak(self._loadEntries(), songId)

    def getSongTimeline(self, songId: str, everyHours: int = 24 * 7) -> list:
        return analyticsLib.songTimeline(self._loadEntries(), songId, everyHours)

    def getCompletionRate(self, songId: str = None, entries: list = None) -> float | None:
        return analyticsLib.completionRate(entries or self._loadEntries(), self._loadTracks(), songId)

    def getIntervalHeatmap(self, startDate: datetime.datetime = None, endDate: datetime.datetime = None, every: int = 24) -> list[tuple[datetime.datetime, datetime.datetime, float, int]]:
        """
        Args:
            startDate: The beginning of the time range to analyze. If
                ``None``, the timestamp of the first entry is used.
            endDate: The end of the time range to analyze. If ``None``,
                all entries after ``startDate`` are included.
            every: The length of each interval in hours.

        Returns:
            - (intervalStart, intervalEnd, totalTimePlayed, playCount) for each interval.
            - ``intervalStart`` is the inclusive start of the interval.
            - ``intervalEnd`` is the exclusive end of the interval.
            - ``totalTimePlayed`` is the sum of ``timePlayed`` for all
            entries in the interval.
            - ``playCount`` is the number of entries in the interval.
        """
        entries = self.filterByInterval(self._loadEntries(), startDate, endDate)
        intervalLength = datetime.timedelta(hours=every)

        if not entries:
            if startDate is None or endDate is None:
                return []

            startDate = startDate.replace(minute=0, second=0, microsecond=0)
            results = []
            intervalStart = startDate
            intervalEnd = intervalStart + intervalLength

            while intervalStart < endDate:
                results.append((intervalStart, intervalEnd, 0, 0))
                intervalStart = intervalEnd
                intervalEnd = intervalStart + intervalLength

            return results

        # entries.sort(key=lambda e: e["playedAt"]) #< Database should already be sorted
        if startDate is None:
            startDate = convertToDatetime(entries[0]["playedAt"])
        startDate = startDate.replace(minute=0, second=0, microsecond=0)

        results = []

        totalTime = 0
        playCount = 0
        intervalEnd = startDate + intervalLength

        for entry in entries:
            playedAt = convertToDatetime(entry["playedAt"])

            while playedAt >= intervalEnd:  #< Needs to be while because there may be gaps in the data, so we need to fill in all the empty intervals
                results.append((startDate, intervalEnd, totalTime, playCount))
                startDate = intervalEnd
                intervalEnd = startDate + intervalLength
                totalTime = 0
                playCount = 0
            totalTime += entry["timePlayed"]
            playCount += 1

        while endDate is not None and intervalEnd < endDate:
            results.append((startDate, intervalEnd, totalTime, playCount))
            startDate = intervalEnd
            intervalEnd = startDate + intervalLength
            totalTime = 0
            playCount = 0

        results.append((startDate, intervalEnd, totalTime, playCount))

        return results

    def startListener(self):
        if self.listener is not None or self.spotify is None:
            return
        self.listener = Listener(self.spotify)
        self.listener.startListener_thread(callback=self._addToDatabaseFromListener)

    def startAutoImporter(self):
        self.autoImporter.start()

    def isListenerLoggedIn(self):
        return self.spotify is not None and self.spotify.isConnected()


if __name__ == "__main__":
    from spotifyApi import SpotifyApi

    manager = Database(user="Tzur", spotifyApi=SpotifyApi(Path(__file__).resolve().parent.parent / "secrets" / ".spotify_token_cache"))
    manager.startListener()
    manager.startAutoImporter()
