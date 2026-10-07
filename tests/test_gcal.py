import json
import os

from conftest import BASE, PROJECT, LogBuilder

from cc_calendar import gcal
from cc_calendar.cli import main
from cc_calendar.parser import SessionAcc

T0 = int(BASE.timestamp() * 1000)
MIN = 60_000
GAP = 15 * MIN
DAY = 24 * 60 * MIN


def session(sid: str, minutes: list[float], branch: str | None = "main") -> SessionAcc:
    b = LogBuilder(sid)
    for i, m in enumerate(minutes):
        b.prompt(m, f"step {i}")
    s = SessionAcc(session_id=sid, path="", project_dir=PROJECT)
    for r in b.records:
        if branch is None:
            r.pop("gitBranch", None)
        s.feed(r)
    return s


def window(*sessions: SessionAcc, gap: int = GAP) -> list[dict]:
    return gcal.desired_events(list(sessions), T0 - DAY, T0 + DAY, gap)


def span(ev: dict) -> tuple[int, int]:
    return gcal.to_ms(ev["start"]), gcal.to_ms(ev["end"])


def test_one_event_per_segment():
    events = window(session("s1", [0, 10, 40, 41]))
    assert [span(e) for e in events] == [
        (T0, T0 + 10 * MIN),
        (T0 + 40 * MIN, T0 + 45 * MIN),  # one minute long, so stretched to five
    ]
    ev = events[0]
    assert ev["key"] == gcal.event_key("s1", T0)
    assert ev["session_id"] == "s1"
    assert ev["summary"] == "[CC] demo (main)"
    assert ev["description"] == f"cc-calendar:{ev['key']}\nsession:s1"


def test_summary_without_branch():
    (ev,) = window(session("s1", [0], branch=None))
    assert ev["summary"] == "[CC] demo"


def test_events_are_sorted_by_start():
    events = window(session("late", [30]), session("early", [0]))
    assert [e["session_id"] for e in events] == ["early", "late"]


def test_key_stays_while_a_segment_grows():
    (before,) = window(session("s1", [0, 10]))
    (after,) = window(session("s1", [0, 10, 20]))
    assert before["key"] == after["key"]
    assert span(after)[1] > span(before)[1]


def test_key_changes_with_the_split():
    s = session("s1", [0, 10, 40])
    split = window(s)
    assert len(split) == 2
    (merged,) = window(s, gap=60 * MIN)
    # The first segment keeps its key and grows; the second one's key is no longer wanted.
    assert merged["key"] == split[0]["key"]
    assert span(merged) == (T0, T0 + 40 * MIN)


def test_window_uses_overlap():
    s = session("s1", [0, 10])
    # The event ends inside the window though it starts before it.
    assert gcal.desired_events([s], T0 + 5 * MIN, T0 + DAY, GAP) == window(s)
    assert not gcal.desired_events([s], T0 + 10 * MIN, T0 + DAY, GAP)
    assert not gcal.desired_events([s], T0 - DAY, T0, GAP)
    # A one-message segment counts with its stretched end.
    assert gcal.desired_events([session("s2", [0])], T0 + 4 * MIN, T0 + DAY, GAP)


def test_old_logs_are_not_read(claude_dir):
    old = claude_dir / "projects" / PROJECT / "s-basic.jsonl"
    os.utime(old, (0, 0))
    sids = {s.session_id for s in gcal.load_sessions([str(claude_dir)], T0)}
    assert "s-basic" not in sids and "s-sub" in sids


def test_json_output(claude_dir, capsys):
    day = BASE.astimezone().date().isoformat()
    main(["events", "--json", "--claude-dir", str(claude_dir), "--since", day, "--until", day])
    out = json.loads(capsys.readouterr().out)
    assert gcal.to_ms(out["timeMin"]) <= T0 < gcal.to_ms(out["timeMax"])
    assert "s-basic" in out["sessions"]
    assert {e["summary"] for e in out["events"]} == {"[CC] demo (main)"}


def test_text_output(claude_dir, capsys):
    day = BASE.astimezone().date().isoformat()
    main(["events", "--claude-dir", str(claude_dir), "--since", day, "--until", day])
    out = capsys.readouterr().out
    assert "[CC] demo (main)" in out
    assert f"from {day} to {day}" in out
