# Causet

**Offline happens-before analyser for message-passing traces.**

Causet reads a plain-text log of sends, receives and local steps, works out
the causal order with Lamport and vector clocks, and tells you what is
concurrent, which cuts are consistent, and where messages were delivered out
of causal order. Standard library only, no network, no accounts.

## Features

- **Plain-text trace format** with one event per line, `#` comments, and events named `<proc>#<n>`.
- **Line-numbered errors**: unknown actions, malformed `send`/`recv`, invalid names, all reported as `file:line: error: ...`.
- **Message pairing lint**: duplicate sends, receives with no send, receives on the wrong process, duplicate receives, self-sends.
- **Circular-wait detection**: a trace where processes wait on each other forever is reported with the blocked lines.
- **Lamport timestamps** for every event.
- **Vector clocks** for every event, shown as text or JSON.
- **Happens-before query**: `relate` says whether two events are before, after, the same, or concurrent.
- **Concurrent-pair listing** for the whole trace.
- **Consistent-cut checker**: given `P=count` frontiers, reports any message received inside the cut whose send lies outside it, and names it.
- **Causal-delivery check**: finds messages delivered out of causal order at the same receiver, including causal predecessors that are never delivered.
- **Unreceived-message report**, which you can silence with `--allow-unreceived`.
- **ASCII space-time diagram** in Lamport order.
- **Graphviz DOT export** with a cluster per process and dashed message edges.
- **CI-friendly exit codes**: `0` clean, `1` findings, `2` invalid trace or bad usage.
- **stdin support**: pass `-` as the file name.

## Trace format

```
# comments start with '#'
C local start            a local step (label is optional)
C send req1 to S        C sends message req1 to process S
S recv req1              S receives req1
```

Lines are program order per process. The order of lines *between* processes
has no meaning; causality comes only from `send`/`recv` pairs.

## Quick start

The repository ships `examples/demo.trace`, a client (`C`), a server (`S`) and a cache (`K`).

```sh
python3 causet.py lint examples/demo.trace
```

```
OK: 12 events, 3 processes, 5 messages
```

```sh
python3 causet.py check examples/demo.trace
```

```
examples/demo.trace:11: unreceived: log1 sent by C to K is never received
1 finding(s)
```

The exit code is `1` here because `log1` is never received. Add
`--allow-unreceived` to accept that and exit `0`.

Ask whether two events are ordered:

```sh
python3 causet.py relate examples/demo.trace C#2 S#1    # C#2 happens-before S#1
python3 causet.py relate examples/demo.trace K#1 C#1    # K#1 is concurrent with C#1
```

## All commands

| Command | What it does |
| --- | --- |
| `lint FILE` | Syntax and message-pairing checks. Exit `2` on errors. |
| `check FILE [--allow-unreceived] [--json]` | Lint, then unreceived and causal-delivery findings. Exit `1` if any. |
| `timestamps FILE [--json]` | Lamport and vector clock for each event. |
| `relate FILE A B` | Before, after, same or concurrent, for events like `P1#2`. |
| `concurrent FILE` | Every pair of concurrent events. |
| `cut FILE P=N ...` | Test a frontier for consistency. Exit `1` if inconsistent. |
| `diagram FILE` | ASCII space-time diagram in Lamport order. |
| `dot FILE` | Graphviz DOT on stdout. Pipe it to `dot -Tsvg`. |

## Running it

Causet needs Python 3 (written for 3.12) and nothing else. Run it from this directory:

```sh
python3 causet.py <command> <file>
```

or from anywhere:

```sh
python3 /path/to/causet.py check trace.txt
```

## Testing

The tests use the standard `unittest` module and also run under `pytest`.

```sh
python3 -m unittest discover -s tests -v
python3 -m pytest -q
```

The tests cover parsing with line numbers, each validation rule, the circular-wait
detector, Lamport and vector clocks, the relation query, the concurrent-pair list,
the consistent-cut check, causal-delivery violations, the diagram and DOT output,
and the CLI exit codes.

## Project layout

```
causet.py              the whole tool (one module, stdlib only)
examples/demo.trace    a sample trace
tests/test_causet.py   unit and CLI tests
conftest.py            empty; lets pytest import causet.py from the root
```

## Limits

- Process names are letters, digits and underscores. Message ids allow `.` and `-` as well.
- Event references use the form `P#n`, so `#` cannot start a process name.
- Traces are finite and checked in full. Analysis is quadratic in the number of events, which is fine for logs in the thousands of events.
