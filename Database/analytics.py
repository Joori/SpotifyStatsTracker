"""Derived listening analytics computed from entries + track metadata."""
from __future__ import annotations

import datetime
from collections import defaultdict
from itertools import combinations

try:
    from Database.utils import convertToDatetime, msToString, dateToString
except ModuleNotFoundError:
    from utils import convertToDatetime, msToString, dateToString

SESSION_GAP_SECONDS = 30 * 60
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _dayKey(dt: datetime.datetime) -> datetime.date:
    return dt.date()


def _monthKey(dt: datetime.datetime) -> str:
    return f"{dt.year}-{dt.month:02d}"


def _weekKey(dt: datetime.datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _weekRangeLabel(weekKey: str) -> str:
    """Turn '2025-W49' into 'Dec 1 - Dec 7, 2025' (ISO week Mon-Sun)."""
    try:
        yearText, weekText = str(weekKey).split("-W", 1)
        start = datetime.date.fromisocalendar(int(yearText), int(weekText), 1)
        end = start + datetime.timedelta(days=6)
        if start.year == end.year and start.month == end.month:
            return f"{start.strftime('%b')} {start.day} - {end.day}, {end.year}"
        if start.year == end.year:
            return f"{start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}"
        return f"{start.strftime('%b %d, %Y')} - {end.strftime('%b %d, %Y')}"
    except Exception:
        return str(weekKey)


def _dayLabel(dayKey) -> str:
    try:
        if hasattr(dayKey, "strftime"):
            return dayKey.strftime("%b %d, %Y")
        return datetime.date.fromisoformat(str(dayKey)).strftime("%b %d, %Y")
    except Exception:
        return str(dayKey)


def buildSessions(entries: list, gapSeconds: int = SESSION_GAP_SECONDS) -> list[list]:
    """Group entries into sessions where consecutive plays are within gapSeconds."""
    if not entries:
        return []
    ordered = sorted(entries, key=lambda e: e.get("playedAt") or 0)
    sessions = []
    current = [ordered[0]]
    for entry in ordered[1:]:
        prev = current[-1].get("playedAt") or 0
        cur = entry.get("playedAt") or 0
        if cur - prev <= gapSeconds:
            current.append(entry)
        else:
            sessions.append(current)
            current = [entry]
    sessions.append(current)
    return sessions


def sessionStats(entries: list, startDate=None, endDate=None) -> dict:
    filtered = _filterEntries(entries, startDate, endDate)
    sessions = buildSessions(filtered)
    if not sessions:
        return {
            "sessionCount": 0,
            "avgSessionMs": 0,
            "avgSessionText": "0:00",
            "longestSessionMs": 0,
            "longestSessionText": "0:00",
            "longestSessionPlays": 0,
            "sessionsPerActiveDay": 0,
            "activeDays": 0,
            "calendarDays": 0,
        }

    sessionLengths = []
    longestMs = 0
    longestPlays = 0
    for session in sessions:
        lengthMs = sum(e.get("timePlayed", 0) for e in session)
        sessionLengths.append(lengthMs)
        if lengthMs > longestMs:
            longestMs = lengthMs
            longestPlays = len(session)

    days = {_dayKey(convertToDatetime(e["playedAt"])) for e in filtered if e.get("playedAt")}
    activeDays = len(days)
    calendarDays = activeDays
    if startDate and endDate:
        calendarDays = max(1, (endDate.date() - startDate.date()).days)
    elif days:
        calendarDays = max(1, (max(days) - min(days)).days + 1)

    avgMs = int(sum(sessionLengths) / len(sessionLengths)) if sessionLengths else 0
    return {
        "sessionCount": len(sessions),
        "avgSessionMs": avgMs,
        "avgSessionText": msToString(avgMs),
        "longestSessionMs": longestMs,
        "longestSessionText": msToString(longestMs),
        "longestSessionPlays": longestPlays,
        "sessionsPerActiveDay": round(len(sessions) / activeDays, 2) if activeDays else 0,
        "activeDays": activeDays,
        "calendarDays": calendarDays,
    }


def streakStats(entries: list, startDate=None, endDate=None, nowDt=None) -> dict:
    """Current and longest consecutive calendar-day streaks (all-time for current)."""
    allDays = sorted({
        _dayKey(convertToDatetime(e["playedAt"]))
        for e in entries if e.get("playedAt")
    })
    if not allDays:
        return {
            "currentStreak": 0,
            "longestStreak": 0,
            "longestInRange": 0,
        }

    def longestRun(days: list) -> int:
        if not days:
            return 0
        best = 1
        run = 1
        for i in range(1, len(days)):
            if (days[i] - days[i - 1]).days == 1:
                run += 1
                best = max(best, run)
            else:
                run = 1
        return best

    longest = longestRun(allDays)
    today = (nowDt or datetime.datetime.now()).date()
    daySet = set(allDays)
    current = 0
    cursor = today
    if today not in daySet:
        cursor = today - datetime.timedelta(days=1)
    while cursor in daySet:
        current += 1
        cursor -= datetime.timedelta(days=1)

    rangeDays = allDays
    if startDate or endDate:
        rangeDays = [
            d for d in allDays
            if (startDate is None or d >= startDate.date())
            and (endDate is None or d < endDate.date())
        ]
    return {
        "currentStreak": current,
        "longestStreak": longest,
        "longestInRange": longestRun(rangeDays),
    }


def timePatterns(entries: list, startDate=None, endDate=None) -> dict:
    filtered = _filterEntries(entries, startDate, endDate)
    byHourPlays = [0] * 24
    byHourMs = [0] * 24
    byDowPlays = [0] * 7
    byDowMs = [0] * 7
    byMonthPlays = [0] * 12
    byMonthMs = [0] * 12

    for entry in filtered:
        if not entry.get("playedAt"):
            continue
        dt = convertToDatetime(entry["playedAt"])
        ms = entry.get("timePlayed", 0)
        byHourPlays[dt.hour] += 1
        byHourMs[dt.hour] += ms
        byDowPlays[dt.weekday()] += 1
        byDowMs[dt.weekday()] += ms
        byMonthPlays[dt.month - 1] += 1
        byMonthMs[dt.month - 1] += ms

    return {
        "hour": {
            "labels": [f"{h:02d}:00" for h in range(24)],
            "plays": byHourPlays,
            "ms": byHourMs,
        },
        "dow": {
            "labels": DAY_NAMES,
            "plays": byDowPlays,
            "ms": byDowMs,
        },
        "month": {
            "labels": MONTH_NAMES,
            "plays": byMonthPlays,
            "ms": byMonthMs,
        },
    }


def monthlyStory(entries: list, tracks: dict, startDate=None, endDate=None) -> list[dict]:
    filtered = _filterEntries(entries, startDate, endDate)
    months = defaultdict(lambda: {
        "songPlays": defaultdict(int),
        "songMs": defaultdict(int),
        "artistPlays": defaultdict(int),
        "artistMs": defaultdict(int),
        "artistMeta": {},
        "songIds": set(),
        "artistNames": set(),
        "totalPlays": 0,
        "totalMs": 0,
    })

    for entry in filtered:
        if not entry.get("playedAt"):
            continue
        dt = convertToDatetime(entry["playedAt"])
        key = _monthKey(dt)
        track = tracks.get(entry.get("id"))
        bucket = months[key]
        bucket["totalPlays"] += 1
        bucket["totalMs"] += entry.get("timePlayed", 0)
        tid = entry.get("id")
        if tid:
            bucket["songIds"].add(tid)
            bucket["songPlays"][tid] += 1
            bucket["songMs"][tid] += entry.get("timePlayed", 0)
        if track:
            for artist in track.get("artists") or []:
                name = artist.get("name")
                if name:
                    bucket["artistNames"].add(name)
                    bucket["artistPlays"][name] += 1
                    bucket["artistMs"][name] += entry.get("timePlayed", 0)
                    existing = bucket["artistMeta"].get(name)
                    if existing is None or (not existing.get("imageId") and artist.get("imageId")):
                        bucket["artistMeta"][name] = {
                            "id": artist.get("id"),
                            "name": name,
                            "imageId": artist.get("imageId"),
                        }

    result = []
    for key in sorted(months.keys()):
        bucket = months[key]
        topSongId = max(bucket["songPlays"], key=bucket["songPlays"].get, default=None)
        topArtistName = max(bucket["artistPlays"], key=bucket["artistPlays"].get, default=None)
        topSong = tracks.get(topSongId, {}) if topSongId else {}
        topArtist = bucket["artistMeta"].get(topArtistName) if topArtistName else None
        if topArtistName and not topArtist:
            topArtist = {"name": topArtistName}

        year, month = key.split("-")
        result.append({
            "key": key,
            "label": f"{MONTH_NAMES[int(month) - 1]} {year}",
            "year": int(year),
            "month": int(month),
            "uniqueSongs": len(bucket["songIds"]),
            "uniqueArtists": len(bucket["artistNames"]),
            "totalPlays": bucket["totalPlays"],
            "totalMs": bucket["totalMs"],
            "totalTimeText": msToString(bucket["totalMs"]),
            "topSong": {
                "id": topSongId,
                "name": topSong.get("name", "Unknown"),
                "imageId": topSong.get("imageId"),
                "plays": bucket["songPlays"].get(topSongId, 0),
            } if topSongId else None,
            "topArtist": {
                "id": topArtist.get("id") if topArtist else None,
                "name": topArtistName,
                "imageId": topArtist.get("imageId") if topArtist else None,
                "plays": bucket["artistPlays"].get(topArtistName, 0),
            } if topArtistName else None,
        })
    return result


def peakObsession(entries: list, tracks: dict, startDate=None, endDate=None, limit: int = 25) -> dict:
    filtered = _filterEntries(entries, startDate, endDate)
    byDay = defaultdict(lambda: defaultdict(int))
    byWeek = defaultdict(lambda: defaultdict(int))

    for entry in filtered:
        if not entry.get("playedAt") or not entry.get("id"):
            continue
        dt = convertToDatetime(entry["playedAt"])
        tid = entry["id"]
        byDay[_dayKey(dt)][tid] += 1
        byWeek[_weekKey(dt)][tid] += 1

    def topBuckets(buckets, kind: str) -> list[dict]:
        ranked = []
        for bucketKey, counts in buckets.items():
            for tid, plays in counts.items():
                track = tracks.get(tid, {})
                if kind == "week":
                    bucketText = _weekRangeLabel(bucketKey)
                else:
                    bucketText = _dayLabel(bucketKey)
                ranked.append({
                    "trackId": tid,
                    "name": track.get("name", "Unknown"),
                    "imageId": track.get("imageId"),
                    "artistsText": ", ".join(
                        a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
                    ),
                    "plays": plays,
                    "bucket": str(bucketKey),
                    "bucketText": bucketText,
                    "kind": kind,
                })
        ranked.sort(key=lambda x: x["plays"], reverse=True)
        return ranked[:limit]

    days = topBuckets(byDay, "day")
    weeks = topBuckets(byWeek, "week")
    return {
        "days": days,
        "weeks": weeks,
        "day": days[0] if days else None,
        "week": weeks[0] if weeks else None,
    }


def songPeak(entries: list, songId: str) -> dict:
    byDay = defaultdict(int)
    byWeek = defaultdict(int)
    for entry in entries:
        if entry.get("id") != songId or not entry.get("playedAt"):
            continue
        dt = convertToDatetime(entry["playedAt"])
        byDay[_dayKey(dt)] += 1
        byWeek[_weekKey(dt)] += 1

    peakDay = max(byDay.items(), key=lambda x: x[1], default=(None, 0))
    peakWeek = max(byWeek.items(), key=lambda x: x[1], default=(None, 0))
    return {
        "peakDay": {"date": str(peakDay[0]) if peakDay[0] else None, "plays": peakDay[1]},
        "peakWeek": {"week": peakWeek[0], "plays": peakWeek[1]},
    }


def trendingSongs(entries: list, tracks: dict, startDate, endDate, limit: int = 10) -> list[dict]:
    if not startDate or not endDate:
        return []
    duration = endDate - startDate
    previousStart = startDate - duration
    previousEnd = startDate

    current = _filterEntries(entries, startDate, endDate)
    previous = _filterEntries(entries, previousStart, previousEnd)

    def counts(chunk):
        c = defaultdict(lambda: {"plays": 0, "ms": 0})
        for e in chunk:
            tid = e.get("id")
            if not tid:
                continue
            c[tid]["plays"] += 1
            c[tid]["ms"] += e.get("timePlayed", 0)
        return c

    curCounts = counts(current)
    prevCounts = counts(previous)
    trending = []
    for tid, stats in curCounts.items():
        prev = prevCounts.get(tid, {"plays": 0, "ms": 0})
        lift = stats["plays"] - prev["plays"]
        if lift <= 0:
            continue
        track = tracks.get(tid, {})
        trending.append({
            "id": tid,
            "name": track.get("name", "Unknown"),
            "imageId": track.get("imageId"),
            "artistsText": ", ".join(
                a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
            ),
            "plays": stats["plays"],
            "previousPlays": prev["plays"],
            "lift": lift,
            "totalTimeListened": stats["ms"],
            "totalTimeListenedText": msToString(stats["ms"]),
        })
    trending.sort(key=lambda x: (x["lift"], x["plays"]), reverse=True)
    return trending[:limit]


def _completionRankings(entries: list, tracks: dict, startDate=None, endDate=None, minPlays: int = 5) -> list[dict]:
    filtered = _filterEntries(entries, startDate, endDate)
    agg = defaultdict(lambda: {"plays": 0, "time": 0, "durationSum": 0})
    for entry in filtered:
        if entry.get("estimated"):   #< live-recorded: listen time is assumed, not measured
            continue
        tid = entry.get("id")
        track = tracks.get(tid)
        if not tid or not track:
            continue
        duration = track.get("duration") or 0
        if duration <= 0:
            continue
        agg[tid]["plays"] += 1
        agg[tid]["time"] += entry.get("timePlayed", 0)
        agg[tid]["durationSum"] += duration

    results = []
    for tid, stats in agg.items():
        if stats["plays"] < minPlays:
            continue
        avgCompletion = stats["time"] / stats["durationSum"] if stats["durationSum"] else 0
        track = tracks.get(tid, {})
        results.append({
            "id": tid,
            "name": track.get("name", "Unknown"),
            "imageId": track.get("imageId"),
            "artistsText": ", ".join(
                a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
            ),
            "plays": stats["plays"],
            "avgCompletion": round(avgCompletion * 100, 1),
            "avgCompletionText": f"{round(avgCompletion * 100, 1)}%",
        })
    return results


def completionUniverse(entries: list, tracks: dict, startDate=None, endDate=None, minPlays: int = 1) -> list[dict]:
    """All songs with completion scores (for client-side min-play filtering)."""
    return _completionRankings(entries, tracks, startDate, endDate, minPlays)


def oftenAbandoned(entries: list, tracks: dict, startDate=None, endDate=None, minPlays: int = 5, limit: int = 10) -> list[dict]:
    results = _completionRankings(entries, tracks, startDate, endDate, minPlays)
    results.sort(key=lambda x: (x["avgCompletion"], -x["plays"]))
    return results[:limit]


def mostCompleted(entries: list, tracks: dict, startDate=None, endDate=None, minPlays: int = 5, limit: int = 10) -> list[dict]:
    results = _completionRankings(entries, tracks, startDate, endDate, minPlays)
    results.sort(key=lambda x: (-x["avgCompletion"], -x["plays"]))
    return results[:limit]


def completionLists(
    entries: list,
    tracks: dict,
    startDate=None,
    endDate=None,
    minPlays: int = 5,
    limit: int = 100,
) -> dict:
    """One aggregation pass → often abandoned + most completed (+ full universe for filtering)."""
    universe = _completionRankings(entries, tracks, startDate, endDate, minPlays=1)
    # Slim payload for client-side filtering (lists don't need art/artists).
    slimUniverse = [
        {
            "id": song["id"],
            "name": song["name"],
            "plays": song["plays"],
            "avgCompletion": song["avgCompletion"],
            "avgCompletionText": song["avgCompletionText"],
        }
        for song in universe
    ]
    filtered = [song for song in slimUniverse if song["plays"] >= minPlays]
    abandoned = sorted(filtered, key=lambda x: (x["avgCompletion"], -x["plays"]))[:limit]
    completed = sorted(filtered, key=lambda x: (-x["avgCompletion"], -x["plays"]))[:limit]
    return {
        "universe": slimUniverse,
        "oftenAbandoned": abandoned,
        "mostCompleted": completed,
    }


def skipStats(entries: list, tracks: dict, startDate=None, endDate=None, minPlays: int = 5, limit: int = 10) -> dict:
    filtered = [e for e in _filterEntries(entries, startDate, endDate) if not e.get("estimated")]   #< no skip info for live-recorded plays
    hasSkipData = any("skipped" in e for e in filtered)
    if not hasSkipData:
        return {"available": False, "mostSkipped": [], "leastSkipped": []}

    agg = defaultdict(lambda: {"plays": 0, "skips": 0})
    for entry in filtered:
        tid = entry.get("id")
        if not tid:
            continue
        agg[tid]["plays"] += 1
        if entry.get("skipped"):
            agg[tid]["skips"] += 1

    scored = []
    for tid, stats in agg.items():
        if stats["plays"] < minPlays:
            continue
        track = tracks.get(tid, {})
        rate = stats["skips"] / stats["plays"]
        scored.append({
            "id": tid,
            "name": track.get("name", "Unknown"),
            "imageId": track.get("imageId"),
            "artistsText": ", ".join(
                a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
            ),
            "plays": stats["plays"],
            "skips": stats["skips"],
            "skipRate": round(rate * 100, 1),
            "skipRateText": f"{round(rate * 100, 1)}%",
        })

    most = sorted(scored, key=lambda x: (x["skipRate"], x["skips"]), reverse=True)[:limit]
    least = sorted(scored, key=lambda x: (x["skipRate"], -x["plays"]))[:limit]
    return {"available": True, "mostSkipped": most, "leastSkipped": least}


def coListening(entries: list, tracks: dict, startDate=None, endDate=None, limit: int = 15, minCount: int = 3) -> dict:
    filtered = _filterEntries(entries, startDate, endDate)
    sessions = buildSessions(filtered)
    pairCounts = defaultdict(int)
    transitionCounts = defaultdict(int)

    for session in sessions:
        ids = [e.get("id") for e in session if e.get("id")]
        unique = list(dict.fromkeys(ids))
        for a, b in combinations(sorted(unique), 2):
            pairCounts[(a, b)] += 1
        for i in range(len(ids) - 1):
            if ids[i] and ids[i + 1] and ids[i] != ids[i + 1]:
                transitionCounts[(ids[i], ids[i + 1])] += 1

    def packPair(pair, count, directed=False):
        a, b = pair
        ta, tb = tracks.get(a, {}), tracks.get(b, {})
        return {
            "a": {"id": a, "name": ta.get("name", "Unknown"), "imageId": ta.get("imageId")},
            "b": {"id": b, "name": tb.get("name", "Unknown"), "imageId": tb.get("imageId")},
            "count": count,
            "directed": directed,
        }

    pairs = [
        packPair(p, c)
        for p, c in sorted(pairCounts.items(), key=lambda x: x[1], reverse=True)
        if c >= minCount
    ][:limit]
    transitions = [
        packPair(p, c, directed=True)
        for p, c in sorted(transitionCounts.items(), key=lambda x: x[1], reverse=True)
        if c >= minCount
    ][:limit]
    return {"pairs": pairs, "transitions": transitions}


def discoveryDays(entries: list, tracks: dict, startDate=None, endDate=None, limit: int = 10) -> dict:
    """Rank days by later play volume of tracks first heard that day."""
    firstHeard = {}
    lifetimePlays = defaultdict(int)
    lifetimeMs = defaultdict(int)

    ordered = sorted(entries, key=lambda e: e.get("playedAt") or 0)
    for entry in ordered:
        tid = entry.get("id")
        if not tid or not entry.get("playedAt"):
            continue
        lifetimePlays[tid] += 1
        lifetimeMs[tid] += entry.get("timePlayed", 0)
        if tid not in firstHeard:
            firstHeard[tid] = convertToDatetime(entry["playedAt"])

    dayScores = defaultdict(lambda: {"legacyPlays": 0, "legacyMs": 0, "trackIds": set()})
    for tid, firstDt in firstHeard.items():
        if startDate and firstDt < startDate:
            continue
        if endDate and firstDt >= endDate:
            continue
        key = _dayKey(firstDt)
        dayScores[key]["legacyPlays"] += lifetimePlays[tid]
        dayScores[key]["legacyMs"] += lifetimeMs[tid]
        dayScores[key]["trackIds"].add(tid)

    ranked = []
    for day, stats in dayScores.items():
        dayTracks = []
        for tid in stats["trackIds"]:
            track = tracks.get(tid, {})
            dayTracks.append({
                "id": tid,
                "name": track.get("name", "Unknown"),
                "imageId": track.get("imageId"),
                "artistsText": ", ".join(
                    a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
                ),
                "plays": lifetimePlays[tid],
                "totalTimeListenedText": msToString(lifetimeMs[tid]),
            })
        dayTracks.sort(key=lambda x: x["plays"], reverse=True)
        ranked.append({
            "date": str(day),
            "dateText": day.strftime("%b %d, %Y"),
            "legacyPlays": stats["legacyPlays"],
            "legacyMs": stats["legacyMs"],
            "legacyTimeText": msToString(stats["legacyMs"]),
            "discoveries": len(stats["trackIds"]),
            "tracks": dayTracks[:100],
        })
    ranked.sort(key=lambda x: (x["legacyPlays"], x["legacyMs"]), reverse=True)

    rangeFirstHears = [
        tid for tid, dt in firstHeard.items()
        if (startDate is None or dt >= startDate) and (endDate is None or dt < endDate)
    ]
    discoveredTracks = []
    for tid in rangeFirstHears:
        track = tracks.get(tid, {})
        discoveredTracks.append({
            "id": tid,
            "name": track.get("name", "Unknown"),
            "imageId": track.get("imageId"),
            "artistsText": ", ".join(
                a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
            ),
            "plays": lifetimePlays[tid],
            "totalTimeListenedText": msToString(lifetimeMs[tid]),
            "firstListenedText": firstHeard[tid].strftime("%b %d, %Y"),
        })
    discoveredTracks.sort(key=lambda x: x["plays"], reverse=True)

    inRangeEntries = _filterEntries(entries, startDate, endDate)
    newPlayShare = 0
    if inRangeEntries and rangeFirstHears:
        newSet = set(rangeFirstHears)
        newPlays = sum(1 for e in inRangeEntries if e.get("id") in newSet)
        newPlayShare = round(newPlays / len(inRangeEntries) * 100, 1)

    return {
        "bestDays": ranked[:limit],
        "newInRange": len(rangeFirstHears),
        "newPlayShare": newPlayShare,
        "topDiscovered": discoveredTracks[:limit],
    }


def concentration(topItems: list, totalMs: int, topN: int = 10) -> float:
    if not totalMs or not topItems:
        return 0.0
    topMs = sum(item.get("totalTimeListened", 0) for item in topItems[:topN])
    return round(topMs / totalMs * 100, 1)


def _trackArtistsText(track: dict) -> str:
    return ", ".join(
        a.get("name", "") for a in (track.get("artists") or []) if isinstance(a, dict)
    )


def _aggregateSessionTracks(session: list, tracks: dict, limit: int = 100) -> list[dict]:
    agg = defaultdict(lambda: {"plays": 0, "ms": 0})
    for entry in session:
        tid = entry.get("id")
        if not tid:
            continue
        agg[tid]["plays"] += 1
        agg[tid]["ms"] += entry.get("timePlayed", 0)
    packed = []
    for tid, stats in agg.items():
        track = tracks.get(tid, {})
        packed.append({
            "id": tid,
            "name": track.get("name", "Unknown"),
            "imageId": track.get("imageId"),
            "artistsText": _trackArtistsText(track),
            "plays": stats["plays"],
            "ms": stats["ms"],
            "totalTimeListenedText": msToString(stats["ms"]),
        })
    packed.sort(key=lambda x: (x["plays"], x["ms"]), reverse=True)
    for item in packed:
        item.pop("ms", None)
    return packed[:limit]


def longestSessions(entries: list, tracks: dict, startDate=None, endDate=None, limit: int = 10) -> list[dict]:
    filtered = _filterEntries(entries, startDate, endDate)
    sessions = buildSessions(filtered)
    packed = []
    for session in sessions:
        if not session or not session[0].get("playedAt"):
            continue
        totalMs = sum(e.get("timePlayed", 0) for e in session)
        startDt = convertToDatetime(session[0]["playedAt"])
        endDt = convertToDatetime(session[-1].get("playedAt") or session[0]["playedAt"])
        songTracks = _aggregateSessionTracks(session, tracks)
        packed.append({
            "dateText": startDt.strftime("%b %d, %Y"),
            "timeRangeText": f"{startDt.strftime('%H:%M')} – {endDt.strftime('%H:%M')}",
            "plays": len(session),
            "uniqueSongs": len({e.get("id") for e in session if e.get("id")}),
            "totalMs": totalMs,
            "totalTimeText": msToString(totalMs),
            "tracks": songTracks,
        })
    packed.sort(key=lambda x: x["totalMs"], reverse=True)
    return packed[:limit]


def heaviestDays(entries: list, tracks: dict, startDate=None, endDate=None, limit: int = 10) -> list[dict]:
    filtered = _filterEntries(entries, startDate, endDate)
    byDay = defaultdict(lambda: {"ms": 0, "plays": 0, "songs": defaultdict(lambda: {"plays": 0, "ms": 0})})
    for entry in filtered:
        if not entry.get("playedAt"):
            continue
        key = _dayKey(convertToDatetime(entry["playedAt"]))
        ms = entry.get("timePlayed", 0)
        byDay[key]["ms"] += ms
        byDay[key]["plays"] += 1
        tid = entry.get("id")
        if tid:
            byDay[key]["songs"][tid]["plays"] += 1
            byDay[key]["songs"][tid]["ms"] += ms
    if not byDay:
        return []

    ranked = []
    for day, stats in byDay.items():
        dayTracks = []
        for tid, songStats in stats["songs"].items():
            track = tracks.get(tid, {})
            dayTracks.append({
                "id": tid,
                "name": track.get("name", "Unknown"),
                "imageId": track.get("imageId"),
                "artistsText": _trackArtistsText(track),
                "plays": songStats["plays"],
                "totalTimeListenedText": msToString(songStats["ms"]),
            })
        dayTracks.sort(key=lambda x: x["plays"], reverse=True)
        ranked.append({
            "date": str(day),
            "dateText": day.strftime("%b %d, %Y"),
            "plays": stats["plays"],
            "totalMs": stats["ms"],
            "totalTimeText": msToString(stats["ms"]),
            "uniqueSongs": len(stats["songs"]),
            "tracks": dayTracks[:100],
        })
    ranked.sort(key=lambda x: x["totalMs"], reverse=True)
    return ranked[:limit]


def heaviestDay(entries: list, startDate=None, endDate=None) -> dict | None:
    days = heaviestDays(entries, {}, startDate, endDate, limit=1)
    if not days:
        return None
    day = days[0]
    return {
        "date": day["date"],
        "dateText": day["dateText"],
        "plays": day["plays"],
        "totalMs": day["totalMs"],
        "totalTimeText": day["totalTimeText"],
    }


def avgDailyListenMs(totalMs: int, activeDays: int) -> int:
    if not activeDays:
        return 0
    return int(totalMs / activeDays)


def completionRate(entries: list, tracks: dict, songId: str | None = None) -> float | None:
    totalPlayed = 0
    totalDuration = 0
    for entry in entries:
        if songId and entry.get("id") != songId:
            continue
        if entry.get("estimated"):
            continue
        track = tracks.get(entry.get("id"))
        if not track:
            continue
        duration = track.get("duration") or 0
        if duration <= 0:
            continue
        totalPlayed += entry.get("timePlayed", 0)
        totalDuration += duration
    if totalDuration <= 0:
        return None
    return round(totalPlayed / totalDuration * 100, 1)


def songTimeline(entries: list, songId: str, everyHours: int = 24 * 7) -> list[dict]:
    songEntries = [e for e in entries if e.get("id") == songId]
    if not songEntries:
        return []
    songEntries.sort(key=lambda e: e.get("playedAt") or 0)
    start = convertToDatetime(songEntries[0]["playedAt"]).replace(minute=0, second=0, microsecond=0)
    end = convertToDatetime(songEntries[-1]["playedAt"]) + datetime.timedelta(hours=everyHours)
    interval = datetime.timedelta(hours=everyHours)
    buckets = []
    cursor = start
    idx = 0
    while cursor < end:
        nextCursor = cursor + interval
        plays = 0
        ms = 0
        while idx < len(songEntries):
            playedAt = convertToDatetime(songEntries[idx]["playedAt"])
            if playedAt >= nextCursor:
                break
            if playedAt >= cursor:
                plays += 1
                ms += songEntries[idx].get("timePlayed", 0)
            idx += 1
        buckets.append({
            "date": dateToString(cursor),
            "label": cursor.strftime("%b %d"),
            "plays": plays,
            "ms": ms,
        })
        cursor = nextCursor
    return buckets


def _filterEntries(entries: list, startDate=None, endDate=None) -> list:
    if startDate is None and endDate is None:
        return list(entries)
    result = []
    for entry in entries:
        playedAt = entry.get("playedAt")
        if playedAt is None:
            continue
        dt = convertToDatetime(playedAt)
        if startDate is not None and dt < startDate:
            continue
        if endDate is not None and dt >= endDate:
            continue
        result.append(entry)
    return result
