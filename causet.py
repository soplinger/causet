"""causet: an offline happens-before analyser for message-passing traces.

A trace is a plain-text file with one event per line:

    P1 local <label>            a local step (label optional)
    P1 send <msg> to <proc>     a message leaves P1 for <proc>
    P1 recv <msg>               P1 receives <msg>

Lines are program order per process; the file order between processes means
nothing. Blank lines and lines starting with '#' are ignored.

Events are named <proc>#<n>, where n counts that process's events from 1.

Commands: lint, check, timestamps, relate, concurrent, cut, diagram, dot.
Exit codes: 0 clean, 1 findings (check, cut), 2 invalid trace or bad usage.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

PROC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MSG_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
REF_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)#(\d+)$")
CUT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(\d+)$")

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


@dataclass
class Event:
    proc: str
    index: int  # 1-based position within its process
    kind: str  # "local", "send" or "recv"
    line: int
    msg: str | None = None
    target: str | None = None
    label: str = ""

    @property
    def ref(self) -> str:
        return f"{self.proc}#{self.index}"


@dataclass
class Message:
    msg: str
    sender: str
    target: str
    send: Event
    recv: Event | None = None


@dataclass
class Trace:
    events: list[Event]
    procs: list[str]  # first-appearance order
    by_proc: dict[str, list[Event]]
    messages: dict[str, Message]
    lamport: dict[str, int] = field(default_factory=dict)
    vc: dict[str, dict[str, int]] = field(default_factory=dict)


# ---------------------------------------------------------------- parsing

def parse_lines(text: str) -> tuple[list[Event], list[tuple[int, str]]]:
    """Syntax check. Returns events and (line, message) errors."""
    events: list[Event] = []
    errors: list[tuple[int, str]] = []
    counts: dict[str, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        proc = tokens[0]
        if not PROC_RE.match(proc):
            errors.append((number, f"invalid process name '{proc}'"))
            continue
        if len(tokens) < 2:
            errors.append((number, "missing action after process name"))
            continue
        action = tokens[1]
        msg = target = None
        label = ""
        if action == "local":
            kind = "local"
            label = " ".join(tokens[2:])
        elif action == "send":
            if len(tokens) != 5 or tokens[3] != "to":
                errors.append((number, "expected: <proc> send <msg> to <proc>"))
                continue
            kind, msg, target = "send", tokens[2], tokens[4]
            if not MSG_RE.match(msg):
                errors.append((number, f"invalid message id '{msg}'"))
                continue
            if not PROC_RE.match(target):
                errors.append((number, f"invalid process name '{target}'"))
                continue
        elif action == "recv":
            if len(tokens) != 3:
                errors.append((number, "expected: <proc> recv <msg>"))
                continue
            kind, msg = "recv", tokens[2]
            if not MSG_RE.match(msg):
                errors.append((number, f"invalid message id '{msg}'"))
                continue
        else:
            errors.append((number, f"unknown action '{action}' (use local, send or recv)"))
            continue
        counts[proc] = counts.get(proc, 0) + 1
        events.append(Event(proc, counts[proc], kind, number, msg, target, label))
    return events, errors


def _semantics(events: list[Event]) -> tuple[dict[str, Message], list[tuple[int, str]]]:
    """Message pairing checks: duplicates, missing sends, wrong receivers."""
    errors: list[tuple[int, str]] = []
    sends: dict[str, Event] = {}
    for e in events:
        if e.kind != "send":
            continue
        if e.msg in sends:
            errors.append((e.line, f"message {e.msg} is already sent on line {sends[e.msg].line}"))
            continue
        sends[e.msg] = e
        if e.target == e.proc:
            errors.append((e.line, f"{e.proc} cannot send {e.msg} to itself"))
    recvs: dict[str, Event] = {}
    for e in events:
        if e.kind != "recv":
            continue
        s = sends.get(e.msg)
        if s is None:
            errors.append((e.line, f"recv of {e.msg} has no matching send"))
            continue
        if e.proc != s.target:
            errors.append((e.line, f"{e.msg} was sent to {s.target} on line {s.line}, not {e.proc}"))
            continue
        if e.msg in recvs:
            errors.append((e.line, f"{e.msg} is already received on line {recvs[e.msg].line}"))
            continue
        recvs[e.msg] = e
    messages = {m: Message(m, s.proc, s.target, s, recvs.get(m)) for m, s in sends.items()}
    return messages, errors


def _compute_clocks(trace: Trace) -> list[Event]:
    """Simulate delivery to assign Lamport and vector clocks.

    Returns events that can never run (a circular wait between processes).
    """
    procs = trace.procs
    ptr = {p: 0 for p in procs}
    clock = {p: dict.fromkeys(procs, 0) for p in procs}
    lam = dict.fromkeys(procs, 0)
    sent: dict[str, tuple[int, dict[str, int]]] = {}
    total = len(trace.events)
    done = 0
    while done < total:
        moved = False
        for p in procs:
            evs = trace.by_proc[p]
            while ptr[p] < len(evs):
                e = evs[ptr[p]]
                if e.kind == "recv":
                    if e.msg not in sent:
                        break
                    sl, sv = sent[e.msg]
                    lam[p] = max(lam[p], sl)
                    for q in procs:
                        clock[p][q] = max(clock[p][q], sv[q])
                lam[p] += 1
                clock[p][p] += 1
                trace.lamport[e.ref] = lam[p]
                trace.vc[e.ref] = dict(clock[p])
                if e.kind == "send":
                    sent[e.msg] = (lam[p], dict(clock[p]))
                ptr[p] += 1
                done += 1
                moved = True
        if not moved:
            break
    stuck = []
    for p in procs:
        if ptr[p] < len(trace.by_proc[p]):
            stuck.append(trace.by_proc[p][ptr[p]])
    return stuck


def load_text(text: str) -> tuple[Trace | None, list[tuple[int, str]]]:
    """Parse, check and analyse. Returns (None, errors) when the trace is invalid."""
    events, errors = parse_lines(text)
    messages, sem_errors = _semantics(events)
    errors += sem_errors
    if errors:
        return None, sorted(errors)

    procs: list[str] = []
    for e in events:
        for name in (e.proc, e.target):
            if name and name not in procs:
                procs.append(name)
    by_proc: dict[str, list[Event]] = {p: [] for p in procs}
    for e in events:
        by_proc[e.proc].append(e)
    trace = Trace(events, procs, by_proc, messages)

    stuck = _compute_clocks(trace)
    if stuck:
        errors = [
            (e.line, f"circular dependency: {e.proc} can never reach this event "
                     f"(processes wait on each other)")
            for e in stuck
        ]
        return None, sorted(errors)
    return trace, []


# ---------------------------------------------------------------- analysis

def relation(trace: Trace, a: str, b: str) -> str:
    """One of: same, before, after, concurrent."""
    if a == b:
        return "same"
    va, vb = trace.vc[a], trace.vc[b]
    if all(va[q] <= vb[q] for q in trace.procs):
        return "before"
    if all(vb[q] <= va[q] for q in trace.procs):
        return "after"
    return "concurrent"


def concurrent_pairs(trace: Trace) -> list[tuple[str, str]]:
    refs = [e.ref for e in trace.events]
    out = []
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            if relation(trace, a, b) == "concurrent":
                out.append((a, b))
    return out


def cut_problems(trace: Trace, frontier: dict[str, int]) -> list[Message]:
    """Messages received inside the cut whose send lies outside it."""
    bad = []
    for m in trace.messages.values():
        if m.recv is None:
            continue
        if m.recv.index <= frontier.get(m.target, 0) and m.send.index > frontier.get(m.sender, 0):
            bad.append(m)
    return bad


def causal_violations(trace: Trace) -> list[tuple[Message, Message]]:
    """Pairs (later, earlier): earlier was sent before later, same receiver,
    but later was delivered first (or earlier never was)."""
    out = []
    msgs = list(trace.messages.values())
    for m1 in msgs:
        for m2 in msgs:
            if m1 is m2 or m1.target != m2.target or m2.recv is None:
                continue
            if relation(trace, m1.send.ref, m2.send.ref) != "before":
                continue
            if m1.recv is None or m2.recv.index < m1.recv.index:
                out.append((m2, m1))
    return out


def describe(e: Event) -> str:
    if e.kind == "send":
        return f"send {e.msg} -> {e.target}"
    if e.kind == "recv":
        return f"recv {e.msg}"
    return f"local {e.label}".rstrip()


def _cell(e: Event) -> str:
    if e.kind == "send":
        return f"send {e.msg}>{e.target}"
    if e.kind == "recv":
        return f"recv {e.msg}"
    return e.label or "local"


def _vector_text(trace: Trace, ref: str) -> str:
    return " ".join(f"{p}={trace.vc[ref][p]}" for p in trace.procs)


def _ordered_events(trace: Trace) -> list[Event]:
    rank = {p: i for i, p in enumerate(trace.procs)}
    return sorted(trace.events, key=lambda e: (trace.lamport[e.ref], rank[e.proc]))


# ---------------------------------------------------------------- rendering

def render_timestamps(trace: Trace) -> str:
    rows = [("EVENT", "LINE", "LAMPORT", "VECTOR", "DESCRIPTION")]
    for e in trace.events:
        rows.append((e.ref, str(e.line), str(trace.lamport[e.ref]),
                     _vector_text(trace, e.ref), describe(e)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = []
    for r in rows:
        head = "  ".join(r[i].ljust(widths[i]) for i in range(4))
        lines.append(f"{head}  {r[4]}".rstrip())
    return "\n".join(lines)


def render_diagram(trace: Trace) -> str:
    procs = trace.procs
    cells = {e.ref: _cell(e) for e in trace.events}
    widths = {}
    for p in procs:
        texts = [cells[e.ref] for e in trace.by_proc[p]] + [p]
        widths[p] = max(len(t) for t in texts) + 2
    header = "LAMP  " + "".join(p.ljust(widths[p]) for p in procs)
    lines = [header.rstrip()]
    for e in _ordered_events(trace):
        row = f"{trace.lamport[e.ref]:>4}  "
        for p in procs:
            text = cells[e.ref] if p == e.proc else "."
            row += text.ljust(widths[p])
        lines.append(row.rstrip())
    return "\n".join(lines)


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def render_dot(trace: Trace) -> str:
    out = ["digraph causet {", "  rankdir=TB;", '  node [shape=box, fontname="monospace"];']
    for p in trace.procs:
        evs = trace.by_proc[p]
        if not evs:
            continue
        out.append(f'  subgraph "cluster_{p}" {{')
        out.append(f'    label="{_dot_escape(p)}";')
        for e in evs:
            label = _dot_escape(e.ref) + "\\n" + _dot_escape(describe(e))
            out.append(f'    "{e.ref}" [label="{label}"];')
        for a, b in zip(evs, evs[1:]):
            out.append(f'    "{a.ref}" -> "{b.ref}";')
        out.append("  }")
    for m in trace.messages.values():
        if m.recv is not None:
            out.append(f'  "{m.send.ref}" -> "{m.recv.ref}" '
                       f'[style=dashed, color=blue, label="{_dot_escape(m.msg)}"];')
    out.append("}")
    return "\n".join(out)


# ---------------------------------------------------------------- CLI

def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load(path: str) -> Trace | None:
    try:
        text = _read(path)
    except OSError as exc:
        print(f"causet: {exc}", file=sys.stderr)
        return None
    trace, problems = load_text(text)
    for line, msg in problems:
        print(f"{path}:{line}: error: {msg}", file=sys.stderr)
    return trace


def _ref(trace: Trace, text: str) -> str:
    m = REF_RE.match(text)
    if not m:
        raise ValueError(f"'{text}' is not an event reference like P1#2")
    proc, n = m.group(1), int(m.group(2))
    count = len(trace.by_proc.get(proc, []))
    if proc not in trace.by_proc or not 1 <= n <= count:
        raise ValueError(f"no event {text} (process {proc} has {count} events)")
    return f"{proc}#{n}"


def cmd_lint(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    print(f"OK: {len(trace.events)} events, {len(trace.procs)} processes, "
          f"{len(trace.messages)} messages")
    return EXIT_OK


def cmd_check(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    findings = []
    if not args.allow_unreceived:
        for m in trace.messages.values():
            if m.recv is None:
                findings.append({
                    "kind": "unreceived", "line": m.send.line,
                    "message": f"{m.msg} sent by {m.sender} to {m.target} is never received"})
    for later, earlier in causal_violations(trace):
        if earlier.recv is None:
            why = f"{earlier.msg} (sent on line {earlier.send.line}) is never delivered"
        else:
            why = f"{earlier.msg} (sent on line {earlier.send.line}) is delivered later"
        findings.append({
            "kind": "causal-delivery", "line": later.recv.line,
            "message": f"{later.msg} is delivered before {why}"})
    findings.sort(key=lambda f: f["line"])
    if args.json:
        print(json.dumps({"ok": not findings, "events": len(trace.events),
                          "findings": findings}, indent=2))
    else:
        for f in findings:
            print(f"{args.file}:{f['line']}: {f['kind']}: {f['message']}")
        print("OK: no findings" if not findings else f"{len(findings)} finding(s)")
    return EXIT_FINDINGS if findings else EXIT_OK


def cmd_timestamps(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    if args.json:
        data = []
        for e in trace.events:
            item = {"event": e.ref, "line": e.line, "action": e.kind,
                    "lamport": trace.lamport[e.ref], "vector": trace.vc[e.ref],
                    "description": describe(e)}
            if e.kind != "local":
                item["message"] = e.msg
            if e.kind == "send":
                item["to"] = e.target
            data.append(item)
        print(json.dumps(data, indent=2))
    else:
        print(render_timestamps(trace))
    return EXIT_OK


def cmd_relate(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    try:
        a, b = _ref(trace, args.a), _ref(trace, args.b)
    except ValueError as exc:
        print(f"causet: {exc}", file=sys.stderr)
        return EXIT_ERROR
    phrases = {"same": "is the same event as", "before": "happens-before",
               "after": "happens-after", "concurrent": "is concurrent with"}
    print(f"{a} {phrases[relation(trace, a, b)]} {b}")
    return EXIT_OK


def cmd_concurrent(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    pairs = concurrent_pairs(trace)
    for a, b in pairs:
        print(f"{a} || {b}")
    print(f"{len(pairs)} concurrent pair(s)")
    return EXIT_OK


def cmd_cut(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    frontier = {p: 0 for p in trace.procs}
    for spec in args.spec:
        m = CUT_RE.match(spec)
        if not m or m.group(1) not in frontier:
            print(f"causet: bad cut '{spec}' (use PROC=COUNT with a known process)", file=sys.stderr)
            return EXIT_ERROR
        proc, n = m.group(1), int(m.group(2))
        if n > len(trace.by_proc[proc]):
            print(f"causet: {proc} has only {len(trace.by_proc[proc])} events", file=sys.stderr)
            return EXIT_ERROR
        frontier[proc] = n
    bad = cut_problems(trace, frontier)
    cut_text = " ".join(f"{p}={frontier[p]}" for p in trace.procs)
    if not bad:
        print(f"consistent cut: {cut_text}")
        return EXIT_OK
    print(f"inconsistent cut: {cut_text}")
    for m in bad:
        print(f"  {m.msg} is received at {m.recv.ref} (line {m.recv.line}) "
              f"but sent at {m.send.ref} (line {m.send.line}) lies outside the cut")
    return EXIT_FINDINGS


def cmd_diagram(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    print(render_diagram(trace))
    return EXIT_OK


def cmd_dot(args) -> int:
    trace = _load(args.file)
    if trace is None:
        return EXIT_ERROR
    print(render_dot(trace))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="causet",
        description="Offline happens-before analyser for message-passing traces.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lint", help="check syntax and message pairing")
    p.add_argument("file")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("check", help="lint plus unreceived and causal-delivery findings")
    p.add_argument("file")
    p.add_argument("--allow-unreceived", action="store_true",
                   help="do not report messages that are never received")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("timestamps", help="Lamport and vector clock for every event")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_timestamps)

    p = sub.add_parser("relate", help="order two events: before, after, concurrent")
    p.add_argument("file")
    p.add_argument("a", metavar="EVENT_A")
    p.add_argument("b", metavar="EVENT_B")
    p.set_defaults(func=cmd_relate)

    p = sub.add_parser("concurrent", help="list every concurrent pair of events")
    p.add_argument("file")
    p.set_defaults(func=cmd_concurrent)

    p = sub.add_parser("cut", help="test whether a frontier PROC=COUNT ... is consistent")
    p.add_argument("file")
    p.add_argument("spec", nargs="+", metavar="PROC=COUNT")
    p.set_defaults(func=cmd_cut)

    p = sub.add_parser("diagram", help="ASCII space-time diagram in Lamport order")
    p.add_argument("file")
    p.set_defaults(func=cmd_diagram)

    p = sub.add_parser("dot", help="Graphviz DOT export")
    p.add_argument("file")
    p.set_defaults(func=cmd_dot)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
