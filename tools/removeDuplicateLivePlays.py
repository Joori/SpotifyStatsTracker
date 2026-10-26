"""
One-off cleanup: remove live-recorded ("estimated") plays that duplicate a play already stored
from a Spotify export. Uses the same matching as the listener and importer: same song, start
within track length + 60s, paired in order.

Dry run (default) only lists what would be removed:
    python tools/removeDuplicateLivePlays.py Database/Users/<user>
Remove them (entries.json is backed up first):
    python tools/removeDuplicateLivePlays.py Database/Users/<user> --apply

Stop the tracker before --apply: it keeps plays in memory and would write the duplicates back
the next time it saves.
"""
import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from Database import playMatching  # noqa: E402


def _fmt(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def run(userFolder, apply=False, out=print):
    """ Returns the duplicate entries found; removes them from entries.json when apply=True. """
    userFolder = Path(userFolder)
    entriesPath = userFolder / "entries.json"
    entries = json.loads(entriesPath.read_text(encoding="utf-8"))
    tracks = json.loads((userFolder / "tracks.json").read_text(encoding="utf-8"))

    real = [e for e in entries if not e.get("estimated")]
    estimated = [e for e in entries if e.get("estimated")]
    pairs = sorted(playMatching.pairWithEstimates(real, estimated, tracks), key=lambda p: p[1]["playedAt"])

    out(f"{len(estimated)} live-recorded play(s), {len(pairs)} duplicate(s) of an exported play:")
    for realEntry, dup in pairs:
        track = tracks.get(dup["id"]) or {}
        artists = ", ".join(a.get("name", "") for a in track.get("artists") or [])
        out(f"  {track.get('name', dup['id'])} - {artists}: live estimate {_fmt(dup['playedAt'])}, "
            f"exported play {_fmt(realEntry['playedAt'])} ({realEntry.get('timePlayed', 0) / 1000:.1f}s played)")

    duplicates = [dup for _, dup in pairs]
    if not apply:
        if duplicates:
            out("Dry run, nothing changed. Run again with --apply to remove them.")
        return duplicates
    if not duplicates:
        return duplicates

    backup = entriesPath.with_name(f"entries.json.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(entriesPath, backup)
    drop = {id(e) for e in duplicates}
    kept = [e for e in entries if id(e) not in drop]
    entriesPath.write_text(json.dumps(kept, indent=4), encoding="utf-8")
    out(f"Removed {len(duplicates)} play(s). Backup: {backup}")
    return duplicates


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remove live plays that duplicate exported plays.")
    parser.add_argument("userFolder", help="folder holding entries.json and tracks.json, e.g. Database/Users/<user>")
    parser.add_argument("--apply", action="store_true", help="actually remove them (default: dry run)")
    args = parser.parse_args()
    run(args.userFolder, apply=args.apply)
