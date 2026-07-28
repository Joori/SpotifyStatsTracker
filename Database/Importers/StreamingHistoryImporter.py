import csv
import json
import datetime
import SpotipyFree
import concurrent.futures

try:
    from Database.Formatters.spotifyClient import Client
    from Database.utils import timeToInt, parseError, now
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from utils import timeToInt, parseError, now


class Importer:
    def __init__(self, user="kjljixx", chunkSize = 1500, prefetchWorkersCount = 16):
        self.sp = SpotipyFree.Spotify()
        self.chunkSize = chunkSize
        self.prefetchWorkersCount = prefetchWorkersCount

    def _searchForSong(self, name, artist):
        query = f"track:{name} artist:{artist}"
        items = self.sp.search(query, type="track", limit=1)["tracks"]["items"]
        if not items:
            raise LookupError(f"No search results for '{name}' by '{artist}'")
        return items[0]

    def _fetchTrackMeta(self, name, artist, trackUri):
        """ Fetch raw track metadata by URI, falling back to a name/artist search. """
        if trackUri:
            try:
                return Client.formatTrack(self.sp.track(trackUri), embedPlaybackInfo=False)
            except Exception as e:
                print(f"URI lookup failed for {trackUri}, falling back to search: {parseError(e)}")
        if not name and not artist:
            return None
        try:
            return Client.formatTrack(self._searchForSong(name=name, artist=artist), embedPlaybackInfo=False)
        except Exception as e:
            print(f"Search failed for '{name}' by '{artist}': {parseError(e)}")
            return None

    def _stubOrNone(self, name, artist, trackUri, timePlayed=0):
        if not name and not artist and not trackUri:
            return None
        return Client.formatStubTrack(name, artist, trackUri, duration=timePlayed)

    def _convertToList(self, export):
        if export.lstrip().startswith("FILE_PATH,"):
            return export.splitlines()[1:], "musicoletPremium"
        try:
            export = json.loads(export)
            if "msPlayed" in export[0]:   #< Acount export
                return export, "spotifyAcountExport"
            if "ts" in export[0]:         #< Extended export
                return export, "spotifyExtendedExport"
        except:
            pass
        return [], "None"

    def importHistory(self, parsedHistory, known, exportType, progressCallback=None):
        if len(parsedHistory) == 0:
            return []
        if exportType == "spotifyAcountExport":
            return self.importAcountHistory(parsedHistory, known=known, progressCallback=progressCallback)
        if exportType == "spotifyExtendedExport":
            return self.importExtendedHistory(parsedHistory, known=known, progressCallback=progressCallback)
        if exportType == "musicoletPremium":
            return self.importMusicoletCSVExport(parsedHistory, known=known, progressCallback=progressCallback)
        return []

    def buildKnownIndex(self, knownTrack):
        index = {}
        for item in knownTrack:
            index[item["id"]] = item
            if len(item["artists"]) == 0:
                continue
            index[item["name"] + item["artists"][0]["name"]] = item
        return index

    def _parseHistory(self, dataFunction, history):
        parsedItems = []
        for item in history:
            try:
                parsed = dataFunction(item)
                if len(parsed) == 5:
                    name, artist, startTimestamp, timePlayed, trackUri = parsed
                    skipped = None
                else:
                    name, artist, startTimestamp, timePlayed, trackUri, skipped = parsed
                parsedItems.append((name, artist, startTimestamp, timePlayed, trackUri, skipped))
            except Exception as e:
                print(f"Error parsing item: {parseError(e)}")
                continue
        return parsedItems

    def _buildTrackId(self, trackUri, name, artist):
        if trackUri:
            return trackUri, True
        if name and artist:
            return name + artist, False
        return None, False

    def _identifyMissingTracks(self, chunk, known):
        missingTracks = {}
        for name, artist, startTimestamp, timePlayed, trackUri, skipped in chunk:
            idKey, isUri = self._buildTrackId(trackUri, name, artist)
            if idKey is None or idKey in known:
                continue

            res = (name, artist, trackUri if isUri else None)
            missingTracks[idKey] = res
        return missingTracks

    def _prefetchMissingTracks(self, missingTracks, chunkStart, totalItems, known, progressCallback):
        totalMissing = len(missingTracks)
        fetchedCount = 0

        def fetchOne(key, info):
            name, artist, trackUri = info
            meta = None
            try:
                meta = self._fetchTrackMeta(name, artist, trackUri)
            except Exception as e:
                print(f"Error fetching {name} by {artist}: {parseError(e)}")
            return key, meta

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.prefetchWorkersCount) as executor:
            futures = {executor.submit(fetchOne, k, val): k for k, val in missingTracks.items()}
            for future in concurrent.futures.as_completed(futures):
                fetchedCount += 1
                if progressCallback:
                    progressCallback(
                        "running",
                        chunkStart + fetchedCount,
                        totalItems,
                        f"Fetching batch metadata ({fetchedCount}/{totalMissing})..."
                    )

                try:
                    key, formatted = future.result()
                    if not formatted:
                        name, artist, trackUri = missingTracks[key]
                        formatted = self._stubOrNone(name, artist, trackUri)
                    if formatted:
                        if key:
                            known[key] = formatted
                        if len(formatted.get("artists") or []) > 0:
                            nameArtistKey = formatted["name"] + formatted["artists"][0]["name"]
                            known[nameArtistKey] = formatted
                except Exception as e:
                    print(f"Error saving fetched track: {parseError(e)}")
        return known

    def _cacheTrack(self, known, base, trackUri, name, artist):
        known[base["id"]] = base
        if trackUri:
            known[trackUri] = base
        if name and artist:
            known[name + artist] = base

    def _resolvePlayMetadata(self, item, known):
        name, artist, startTimestamp, timePlayed, trackUri, skipped = item
        try:
            matchedId, isUri = self._buildTrackId(trackUri, name, artist)
            if matchedId and matchedId in known:
                meta = Client.embedPlayInfo(known[matchedId].copy(), startTimestamp, timePlayed, skipped=skipped)
            else:
                if not name and not artist and not trackUri:
                    return None

                base = self._fetchTrackMeta(name, artist, trackUri)
                if not base:
                    base = self._stubOrNone(name, artist, trackUri, timePlayed)
                if not base:
                    return None

                self._cacheTrack(known, base, trackUri, name, artist)
                meta = Client.embedPlayInfo(base.copy(), startTimestamp, timePlayed, skipped=skipped)

            return meta
        except Exception as e:
            print(f"Error processing item: {parseError(e)}")
            base = self._stubOrNone(name, artist, trackUri, timePlayed)
            if not base:
                return None
            try:
                return Client.embedPlayInfo(base.copy(), startTimestamp, timePlayed, skipped=skipped)
            except Exception as embedError:
                print(f"Error embedding stub play: {parseError(embedError)}")
                return None

    def _import(self, dataFunction, history, known=[], progressCallback=None):
        known = self.buildKnownIndex(known)

        parsedItems = self._parseHistory(dataFunction, history)
        totalItems = len(parsedItems)
        if totalItems == 0:
            return

        for chunkStart in range(0, totalItems, self.chunkSize):
            chunk = parsedItems[chunkStart: chunkStart + self.chunkSize]

            missingTracks = self._identifyMissingTracks(chunk, known)

            if missingTracks:
                known = self._prefetchMissingTracks(  #< get metadata for missing tracks in parallel
                    missingTracks,
                    chunkStart,
                    totalItems,
                    known,
                    progressCallback
                )

            for item in chunk:
                meta = self._resolvePlayMetadata(item, known)
                if meta:
                    yield meta

    def importAcountHistory(self, history, known=[], progressCallback=None):
        def dataFunction(item):
            endTimestamp = timeToInt(item["endTime"])
            timePlayed = item["msPlayed"]

            startTimestamp = endTimestamp - timePlayed // 1000
            name = item["trackName"]
            artist = item["artistName"]
            return name, artist, startTimestamp, timePlayed, None, None

        yield from self._import(dataFunction, history, known, progressCallback)

    def importExtendedHistory(self, history, known=[], progressCallback=None):
        def dataFunction(item):
            ts = item["ts"]
            endTimestamp = timeToInt(ts)
            timePlayed = item.get("ms_played", 0)
            startTimestamp = endTimestamp - timePlayed // 1000

            name = item["master_metadata_track_name"]
            artist = item["master_metadata_album_artist_name"]
            uri = item.get("spotify_track_uri")
            trackUri = uri.split(":")[-1] if uri else None
            skipped = item.get("skipped")
            if skipped is None and item.get("reason_end") == "fwdbtn":
                skipped = True
            return name, artist, startTimestamp, timePlayed, trackUri, skipped

        yield from self._import(dataFunction, history, known, progressCallback)

    def importMusicoletCSVExport(self, rows, known=[], progressCallback=None):
        def expand(rows):
            ### Data formatted in: FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT
            NAME = 1
            ARTISTS = 2
            DURATION_MS = 8
            PLAYCOUNT = 9

            currentTime = now()
            formatedData = []
            reader = csv.reader(rows)
            
            for song in reader:
                if not song:
                    continue
                    
                try:
                    name = song[NAME]
                    mainArtist = song[ARTISTS].split("/")[0]
                    timePlayed = int(song[DURATION_MS])
                    playCount = int(song[PLAYCOUNT])
                    
                    for _ in range(playCount):
                        startTimestamp = currentTime.strftime("%Y-%m-%d %H:%M:%S")
                        formatedData.append((
                            name,
                            mainArtist,
                            startTimestamp,
                            timePlayed
                        ))
                        currentTime += datetime.timedelta(milliseconds=timePlayed)
                        
                except (IndexError, ValueError) as e:
                    continue
                    
            return formatedData

        def dataFunction(item):
            name, mainArtist, startTimestamp, timePlayed = item
            return name, mainArtist, startTimestamp, timePlayed, None, None

        yield from self._import(dataFunction, expand(rows), known, progressCallback)