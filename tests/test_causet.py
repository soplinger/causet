import contextlib
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import causet  # noqa: E402

CHAIN = """\
P1 local start
P1 send m1 to P2
P2 recv m1
P2 send m2 to P1
P1 recv m2
"""

INDEPENDENT = """\
P1 local a
P2 local b
"""

DEADLOCK = """\
P1 recv b
P1 send a to P2
P2 recv a
P2 send b to P1
"""

OUT_OF_ORDER = """\
P1 send m1 to P3
P1 send m2 to P3
P3 recv m2
P3 recv m1
"""

IN_ORDER = """\
P1 send m1 to P3
P1 send m2 to P3
P3 recv m1
P3 recv m2
"""

UNRECEIVED = """\
P1 local start
P1 send x to P2
"""


def load(text):
    trace, problems = causet.load_text(text)
    return trace, problems


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = causet.main(argv)
    return code, out.getvalue(), err.getvalue()


def write_temp(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".trace", delete=False, encoding="utf-8")
    handle.write(text)
    handle.close()
    return handle.name


class ParseTests(unittest.TestCase):
    def test_valid_trace_parses_all_events(self):
        events, errors = causet.parse_lines(CHAIN)
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 5)
        self.assertEqual(events[1].ref, "P1#2")
        self.assertEqual(events[1].kind, "send")
        self.assertEqual(events[1].target, "P2")

    def test_unknown_action_reports_its_line(self):
        _, errors = causet.parse_lines("P1 local ok\nP1 shout hello\n")
        self.assertEqual(errors, [(2, "unknown action 'shout' (use local, send or recv)")])

    def test_send_without_to_reports_its_line(self):
        _, errors = causet.parse_lines("P1 local x\n\nP1 send m1 P2\n")
        self.assertEqual(errors[0][0], 3)
        self.assertIn("expected: <proc> send <msg> to <proc>", errors[0][1])

    def test_bad_process_name_reports_line(self):
        _, errors = causet.parse_lines("1P local x\n")
        self.assertEqual(errors[0][0], 1)
        self.assertIn("invalid process name", errors[0][1])

    def test_comments_and_blank_lines_are_skipped(self):
        events, errors = causet.parse_lines("# note\n\n   \nP1 local a\n")
        self.assertEqual(errors, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].line, 4)


class ValidationTests(unittest.TestCase):
    def test_duplicate_send_is_flagged_on_second_line(self):
        _, problems = load("P1 send m1 to P2\nP1 send m1 to P2\nP2 recv m1\n")
        self.assertEqual(problems[0][0], 2)
        self.assertIn("already sent on line 1", problems[0][1])

    def test_recv_without_send_is_flagged(self):
        _, problems = load("P2 recv ghost\n")
        self.assertEqual(problems, [(1, "recv of ghost has no matching send")])

    def test_recv_on_wrong_process_is_flagged(self):
        _, problems = load("P1 send m1 to P2\nP3 recv m1\n")
        self.assertEqual(problems[0][0], 2)
        self.assertIn("sent to P2 on line 1, not P3", problems[0][1])

    def test_duplicate_recv_is_flagged(self):
        _, problems = load("P1 send m1 to P2\nP2 recv m1\nP2 recv m1\n")
        self.assertEqual(problems[0][0], 3)

    def test_circular_wait_is_reported_as_deadlock(self):
        trace, problems = load(DEADLOCK)
        self.assertIsNone(trace)
        self.assertEqual(sorted(line for line, _ in problems), [1, 3])
        self.assertTrue(all("circular dependency" in msg for _, msg in problems))


class ClockTests(unittest.TestCase):
    def test_lamport_timestamps(self):
        trace, problems = load(CHAIN)
        self.assertEqual(problems, [])
        self.assertEqual(trace.lamport["P1#1"], 1)
        self.assertEqual(trace.lamport["P1#2"], 2)
        self.assertEqual(trace.lamport["P2#1"], 3)
        self.assertEqual(trace.lamport["P2#2"], 4)
        self.assertEqual(trace.lamport["P1#3"], 5)

    def test_vector_clocks_track_received_knowledge(self):
        trace, _ = load(CHAIN)
        self.assertEqual(trace.vc["P2#1"], {"P1": 2, "P2": 1})
        self.assertEqual(trace.vc["P1#3"], {"P1": 3, "P2": 2})

    def test_relation_before_after_and_same(self):
        trace, _ = load(CHAIN)
        self.assertEqual(causet.relation(trace, "P1#1", "P2#1"), "before")
        self.assertEqual(causet.relation(trace, "P2#1", "P1#2"), "after")
        self.assertEqual(causet.relation(trace, "P1#2", "P1#2"), "same")

    def test_independent_events_are_concurrent(self):
        trace, _ = load(INDEPENDENT)
        self.assertEqual(causet.relation(trace, "P1#1", "P2#1"), "concurrent")
        self.assertEqual(causet.concurrent_pairs(trace), [("P1#1", "P2#1")])

    def test_totally_ordered_trace_has_no_concurrent_pairs(self):
        trace, _ = load(CHAIN)
        self.assertEqual(causet.concurrent_pairs(trace), [])


class AnalysisTests(unittest.TestCase):
    def test_consistent_cut_passes(self):
        trace, _ = load(CHAIN)
        self.assertEqual(causet.cut_problems(trace, {"P1": 2, "P2": 1}), [])

    def test_cut_including_receive_without_send_names_message(self):
        trace, _ = load(CHAIN)
        bad = causet.cut_problems(trace, {"P1": 1, "P2": 1})
        self.assertEqual([m.msg for m in bad], ["m1"])

    def test_out_of_order_delivery_is_a_causal_violation(self):
        trace, _ = load(OUT_OF_ORDER)
        violations = causet.causal_violations(trace)
        self.assertEqual(len(violations), 1)
        later, earlier = violations[0]
        self.assertEqual((later.msg, earlier.msg), ("m2", "m1"))

    def test_in_order_delivery_has_no_violation(self):
        trace, _ = load(IN_ORDER)
        self.assertEqual(causet.causal_violations(trace), [])

    def test_unreceived_message_is_recorded_with_no_recv(self):
        trace, _ = load(UNRECEIVED)
        self.assertIsNone(trace.messages["x"].recv)


class RenderTests(unittest.TestCase):
    def test_diagram_has_header_and_lamport_rows(self):
        trace, _ = load(CHAIN)
        lines = causet.render_diagram(trace).splitlines()
        self.assertTrue(lines[0].startswith("LAMP"))
        self.assertIn("P1", lines[0])
        self.assertIn("send m1>P2", lines[2])
        self.assertEqual(len(lines), 1 + 5)

    def test_dot_contains_process_and_message_edges(self):
        trace, _ = load(CHAIN)
        dot = causet.render_dot(trace)
        self.assertIn('"P1#2" -> "P2#1"', dot)
        self.assertIn('"P1#1" -> "P1#2"', dot)
        self.assertTrue(dot.startswith("digraph causet {"))
        self.assertTrue(dot.rstrip().endswith("}"))


class CliTests(unittest.TestCase):
    def test_check_clean_trace_exits_zero(self):
        path = write_temp(CHAIN)
        try:
            code, out, _ = run_cli(["check", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_OK)
        self.assertIn("OK: no findings", out)

    def test_check_unreceived_message_exits_one(self):
        path = write_temp(UNRECEIVED)
        try:
            code, out, _ = run_cli(["check", path])
            allowed, _, _ = run_cli(["check", "--allow-unreceived", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_FINDINGS)
        self.assertIn("unreceived", out)
        self.assertEqual(allowed, causet.EXIT_OK)

    def test_invalid_trace_exits_two_with_file_line(self):
        path = write_temp("P1 local ok\nP1 shout nope\n")
        try:
            code, _, err = run_cli(["lint", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_ERROR)
        self.assertIn(":2: error: unknown action", err)

    def test_relate_reports_concurrency(self):
        path = write_temp(INDEPENDENT)
        try:
            code, out, _ = run_cli(["relate", path, "P1#1", "P2#1"])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_OK)
        self.assertEqual(out.strip(), "P1#1 is concurrent with P2#1")

    def test_relate_rejects_unknown_event(self):
        path = write_temp(CHAIN)
        try:
            code, _, err = run_cli(["relate", path, "P1#1", "P9#1"])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_ERROR)
        self.assertIn("no event", err)

    def test_cut_inconsistent_exits_one(self):
        path = write_temp(CHAIN)
        try:
            code, out, _ = run_cli(["cut", path, "P1=1", "P2=1"])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_FINDINGS)
        self.assertIn("m1 is received", out)

    def test_timestamps_json_is_parseable(self):
        import json
        path = write_temp(CHAIN)
        try:
            code, out, _ = run_cli(["timestamps", "--json", path])
        finally:
            os.unlink(path)
        self.assertEqual(code, causet.EXIT_OK)
        data = json.loads(out)
        self.assertEqual(data[0]["event"], "P1#1")
        self.assertEqual(data[2]["lamport"], 3)


if __name__ == "__main__":
    unittest.main()
