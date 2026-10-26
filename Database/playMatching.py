"""
Matching plays that were recorded twice: once live (recently-played listener, timestamps
estimated from played_at - track length, flagged "estimated") and once from a Spotify
history export (exact start time and ms played).

The two timestamps for the same play can differ by up to the track length, so plays are
paired per song, in time order, when they are within (track length + SLACK_SECONDS).
Pairing in order means back-to-back repeats of the same song pair up 1:1 instead of
several imports grabbing the same live play.
"""

SLACK_SECONDS = 60
MIN_LIVE_PLAY_MS = 30_000     #< recently-played only lists plays of 30s or more
EXACT_TOLERANCE_SECONDS = 2


def songKey(track):
    """ Same song even if Spotify gave a different track id (relinking, re-releases). """
    if not track:
        return None
    name = (track.get("name") or "").strip().lower()
    artists = track.get("artists") or []
    artist = (artists[0].get("name") or "").strip().lower() if artists and isinstance(artists[0], dict) else ""
    if not name:
        return None
    return f"{name}|{artist}"


def _entryKey(entry, tracks):
    return songKey(tracks.get(entry.get("id"))) or entry.get("id")


def _durationSeconds(entryId, tracks, fallbackMs=0):
    track = tracks.get(entryId) or {}
    return int((track.get("duration") or fallbackMs or 0) // 1000)


def matchInOrder(left, right, slack=SLACK_SECONDS):
    """
    left/right: lists of (timestamp, durationSeconds, payload), each sorted by timestamp.
    Returns [(leftPayload, rightPayload)] pairs, each item used at most once.
    """
    pairs = []
    i = j = 0
    while i < len(left) and j < len(right):
        lt, ld, lp = left[i]
        rt, rd, rp = right[j]
        if abs(lt - rt) <= max(ld, rd) + slack:
            pairs.append((lp, rp))
            i += 1
            j += 1
        elif lt < rt:
            i += 1
        else:
            j += 1
    return pairs


def _group(items):
    groups = {}
    for key, item in items:
        if key is None:
            continue
        groups.setdefault(key, []).append(item)
    for values in groups.values():
        values.sort(key=lambda x: x[0])
    return groups


def mergeImported(existingEntries, importedEntries, tracks):
    """
    Decide how imported plays combine with what is already stored.

    Returns (estimatesToReplace, entriesToAdd, duplicatesSkipped):
      - estimatesToReplace: existing estimated entries that the import covers (remove them)
      - entriesToAdd: imported entries that are not exact duplicates of stored real plays
      - duplicatesSkipped: count of imported plays already stored (same song, same start)
    """
    realStarts = {}
    estimated = []
    for entry in existingEntries:
        if entry.get("estimated"):
            estimated.append(entry)
        else:
            realStarts.setdefault(entry.get("id"), set()).add(entry.get("playedAt"))

    entriesToAdd = []
    duplicatesSkipped = 0
    for entry in importedEntries:
        starts = realStarts.get(entry.get("id"), ())
        playedAt = entry.get("playedAt") or 0
        if any(abs(playedAt - s) <= EXACT_TOLERANCE_SECONDS for s in starts if s is not None):
            duplicatesSkipped += 1
            continue
        entriesToAdd.append(entry)

    importedGroups = _group(
        (_entryKey(e, tracks), (e.get("playedAt") or 0, _durationSeconds(e.get("id"), tracks, e.get("timePlayed")), e))
        for e in entriesToAdd
        if (e.get("timePlayed") or 0) >= MIN_LIVE_PLAY_MS
    )
    estimatedGroups = _group(
        (_entryKey(e, tracks), (e.get("playedAt") or 0, _durationSeconds(e.get("id"), tracks, e.get("timePlayed")), e))
        for e in estimated
    )

    estimatesToReplace = []
    for key, imported in importedGroups.items():
        for _, estimate in matchInOrder(imported, estimatedGroups.get(key, [])):
            estimatesToReplace.append(estimate)

    return estimatesToReplace, entriesToAdd, duplicatesSkipped


def filterNewLivePlays(candidates, existingEntries, tracks):
    """
    candidates: list of dicts with "id", "playedAt" (estimated start), "duration" (ms), "key".
    Drops candidates already stored: either the same estimated play from an earlier poll
    (identical estimate) or a real play from an imported export (paired in order).
    """
    estimatedStarts = {}
    real = []
    for entry in existingEntries:
        if entry.get("estimated"):
            estimatedStarts.setdefault(entry.get("id"), []).append(entry.get("playedAt") or 0)
        elif (entry.get("timePlayed") or 0) >= MIN_LIVE_PLAY_MS:
            real.append(entry)

    remaining = []
    for candidate in candidates:
        starts = estimatedStarts.get(candidate["id"], [])
        if any(abs(candidate["playedAt"] - s) <= EXACT_TOLERANCE_SECONDS for s in starts):
            continue
        remaining.append(candidate)

    candidateGroups = _group(
        (c["key"] or c["id"], (c["playedAt"], int((c.get("duration") or 0) // 1000), c)) for c in remaining
    )
    realGroups = _group(
        (_entryKey(e, tracks), (e.get("playedAt") or 0, _durationSeconds(e.get("id"), tracks, e.get("timePlayed")), e))
        for e in real
    )
    alreadyStored = set()
    for key, group in candidateGroups.items():
        for candidate, _ in matchInOrder(group, realGroups.get(key, [])):
            alreadyStored.add(id(candidate))

    return [c for c in remaining if id(c) not in alreadyStored]
