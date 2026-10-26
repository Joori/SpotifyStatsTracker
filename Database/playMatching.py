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


def _timed(entries, tracks, keyFor=None):
    keyFor = keyFor or (lambda e: _entryKey(e, tracks))
    return _group(
        (keyFor(e), (e.get("playedAt") or 0, _durationSeconds(e.get("id"), tracks, e.get("timePlayed")), e))
        for e in entries
    )


def pairWithEstimates(realEntries, estimatedEntries, tracks):
    """ [(real, estimate)] for each estimated play a real play covers: same song, paired in order. """
    estimatedGroups = _timed(estimatedEntries, tracks)
    pairs = []
    for key, real in _timed(realEntries, tracks).items():
        pairs.extend(matchInOrder(real, estimatedGroups.get(key, [])))
    return pairs


def duplicateEstimates(entries, tracks):
    """ Estimated (live) plays that duplicate a real play already stored. """
    real = [e for e in entries if not e.get("estimated")]
    estimated = [e for e in entries if e.get("estimated")]
    return [estimate for _, estimate in pairWithEstimates(real, estimated, tracks)]


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

    # Short plays count too: recently-played does list some plays under 30s
    estimatesToReplace = [estimate for _, estimate in pairWithEstimates(entriesToAdd, estimated, tracks)]
    return estimatesToReplace, entriesToAdd, duplicatesSkipped


def filterNewLivePlays(candidates, existingEntries, tracks):
    """
    candidates: list of dicts with "id", "playedAt" (estimated start), "duration" (ms), "key".
    Drops candidates that are already stored, imported or live, using the importer's rule:
    same song, start within track length + SLACK_SECONDS, paired in order.
    """
    # The same live play seen again by a later poll has an identical estimate. Pair those first,
    # so back-to-back repeats of a song can't fuzzy-pair with the wrong earlier play.
    estimatedById = {}
    for entry in existingEntries:
        if entry.get("estimated"):
            estimatedById.setdefault(entry.get("id"), []).append(entry)
    used = set()
    remaining = []
    for candidate in candidates:
        same = next((e for e in estimatedById.get(candidate["id"], ())
                     if id(e) not in used and abs(candidate["playedAt"] - (e.get("playedAt") or 0)) <= EXACT_TOLERANCE_SECONDS), None)
        if same is not None:
            used.add(id(same))
            continue
        remaining.append(candidate)

    # A stored play with the same track id is the same song even if its saved metadata
    # (name/artist) is out of date, so key it like the candidate.
    keyById = {c["id"]: c["key"] or c["id"] for c in remaining}
    stored = [e for e in existingEntries if id(e) not in used]
    storedGroups = _timed(stored, tracks, keyFor=lambda e: keyById.get(e.get("id")) or _entryKey(e, tracks))
    candidateGroups = _group(
        (c["key"] or c["id"], (c["playedAt"], int((c.get("duration") or 0) // 1000), c)) for c in remaining
    )
    alreadyStored = set()
    for key, group in candidateGroups.items():
        for candidate, _ in matchInOrder(group, storedGroups.get(key, [])):
            alreadyStored.add(id(candidate))

    return [c for c in remaining if id(c) not in alreadyStored]
