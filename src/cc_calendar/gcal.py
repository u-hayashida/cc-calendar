"""`cc-calendar events`: the calendar events that the active stretches of sessions should become.

Each segment of a session (see `SessionAcc.segments`) becomes one event titled with the project
and branch only; titles and prompts are left out. cc-calendar does not contact Google itself:
`--json` prints the events, and an agent with a Google Calendar connector writes them.

Each event carries a key in its description, so a re-run can match the events already in the
calendar: the key stays the same while a segment grows, and changes when it is split differently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .parser import SessionAcc
from .server import project_name
from .store import Store

MIN_EVENT_MS = 5 * 60_000
MTIME_SLACK_MS = 24 * 60 * 60_000
KEY_PREFIX = "cc-calendar:"
SESSION_PREFIX = "session:"


def event_key(session_id: str, start_ms: int) -> str:
    return hashlib.sha1(f"{session_id}:{start_ms}".encode()).hexdigest()


def summary_of(s: SessionAcc) -> str:
    name = project_name(s)
    return f"[CC] {name} ({s.git_branch})" if s.git_branch else f"[CC] {name}"


def rfc3339(ms: int) -> str:
    # .astimezone() uses the system local timezone. This is intentional: the output
    # is RFC 3339 with an explicit offset, which Google Calendar accepts regardless of
    # which offset is used. CI running in UTC will produce "+00:00" instead of "+09:00",
    # but the timestamps are equivalent.
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def to_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def desired_events(
    sessions: list[SessionAcc], since_ms: int, until_ms: int, gap_ms: int
) -> list[dict]:
    """One event per segment that overlaps [since, until), in start order.

    Overlap is the rule Google uses to list events in a time range, so an event that crosses
    the boundary is both listed and wanted, and is never deleted by mistake.
    """
    out: list[dict] = []
    for s in sessions:
        for seg_start, seg_end in s.segments(gap_ms):
            # Google keeps seconds only; round here so a re-run compares equal.
            start = seg_start // 1000 * 1000
            end = max(seg_end // 1000 * 1000, start + MIN_EVENT_MS)
            if not (end > since_ms and start < until_ms):
                continue
            key = event_key(s.session_id, seg_start)
            out.append(
                {
                    "key": key,
                    "session_id": s.session_id,
                    "summary": summary_of(s),
                    "start": rfc3339(start),
                    "end": rfc3339(end),
                    "description": f"{KEY_PREFIX}{key}\n{SESSION_PREFIX}{s.session_id}",
                }
            )
    out.sort(key=lambda e: (to_ms(e["start"]), e["key"]))
    return out


def day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from None


def local_ms(d: date) -> int:
    return int(datetime.combine(d, time()).astimezone().timestamp() * 1000)


def load_sessions(specs: list[str], since_ms: int) -> list[SessionAcc]:
    """Sessions whose log was written to at or after `since_ms` (less a day of slack).

    Older logs cannot hold activity in the window, so they are not read at all. Records are
    appended as they happen, so a log's mtime is no earlier than its newest record, except
    when the log was written on another machine whose clock runs ahead (a synced
    --claude-dir); the slack covers that.
    """
    from .cli import claude_dirs

    store = Store(claude_dirs(specs))
    for d in store.dirs:
        if not d.projects_dir.is_dir():
            continue
        for path in sorted(d.projects_dir.glob("*/*.jsonl")):
            try:
                if path.stat().st_mtime * 1000 < since_ms - MTIME_SLACK_MS:
                    continue
            except OSError:
                continue
            store.update_file(path)
            session_dir = path.with_suffix("")
            for sub in sorted(session_dir.glob("subagents/*.meta.json")):
                store.update_file(sub)
            for sub in sorted(session_dir.glob("subagents/*.jsonl")):
                store.update_file(sub)
    return list(store.sessions.values())


def events_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="cc-calendar events",
        description="List the calendar events for the active stretches of your sessions: "
        "one per stretch, titled with the project and branch.",
    )
    today = date.today()
    parser.add_argument(
        "--since", type=day, default=today, help="first day, YYYY-MM-DD (default: today)"
    )
    parser.add_argument(
        "--until", type=day, default=today, help="last day, YYYY-MM-DD (default: today)"
    )
    # Note: --until defaults to *today* independently of --since.
    # If only --since is given (e.g. --since 2026-10-01), --until stays as today,
    # which is likely the intended behaviour: "from a past date up to today".
    parser.add_argument(
        "--claude-dir",
        action="append",
        metavar="[NAME=]PATH",
        help="Claude Code config directory to read; repeat for several (default: ~/.claude)",
    )
    parser.add_argument(
        "--gap",
        type=int,
        default=15,
        metavar="MINUTES",
        help="idle minutes that split a session into separate events (default: 15)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON for a calendar agent")
    args = parser.parse_args(argv)
    if args.gap < 1:
        parser.error("--gap must be at least 1")
    if args.until < args.since:
        parser.error("--until is before --since")

    since_ms = local_ms(args.since)
    until_ms = local_ms(args.until + timedelta(days=1))
    sessions = load_sessions(args.claude_dir or [str(Path.home() / ".claude")], since_ms)
    events = desired_events(sessions, since_ms, until_ms, args.gap * 60_000)

    if args.json:
        out = {
            "timeMin": rfc3339(since_ms),
            "timeMax": rfc3339(until_ms),
            # An existing event may be deleted only if its session is listed here.
            "sessions": sorted(s.session_id for s in sessions),
            "events": events,
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    for ev in events:
        start = datetime.fromisoformat(ev["start"])
        end = datetime.fromisoformat(ev["end"])
        print(f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}  {ev['summary']}")
    print(f"{len(events)} events from {args.since} to {args.until}")
