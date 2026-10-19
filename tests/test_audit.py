"""Tests for the cut-context audit. Standard library only: python3 -m unittest discover tests"""

import ast
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "..", "plugins", "cut-context", "skills", "audit", "audit.py")
sys.path.insert(0, os.path.dirname(AUDIT))
import audit  # noqa: E402


def usage(read=0, write=0, out=0, inp=0, ttl="1h"):
    cc = {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0} if ttl == "1h" else \
         {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": write}
    return {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
            "output_tokens": out, "cache_creation": cc}


class Log:
    """Builds a session log the way Claude Code writes it."""

    def __init__(self):
        self.lines = []
        self.n = 0

    def request(self, u, tool=None, ts="2026-10-01T10:00:00Z", blocks=1):
        self.n += 1
        rid = "req_%d" % self.n
        content = [{"type": "text", "text": "ok"}]
        if tool:
            content.append({"type": "tool_use", "id": tool[0], "name": tool[1], "input": tool[2]})
        # a request is often split over several records that repeat the usage
        for _ in range(blocks):
            self.lines.append({"type": "assistant", "requestId": rid, "timestamp": ts, "cwd": "/tmp/proj",
                               "version": "2.1.300",
                               "message": {"model": "claude-test", "role": "assistant", "content": content, "usage": u}})
        return self

    def result(self, tool_id, text="", images=0):
        body = [{"type": "text", "text": text}] + [{"type": "image", "source": {"data": "x" * 50000}}] * images
        self.lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": body}]}})
        return self

    def compact(self):
        self.lines.append({"type": "system", "subtype": "compact_boundary"})
        return self

    def write(self, folder, name="s1.jsonl"):
        p = os.path.join(folder, name)
        with open(p, "w") as fh:
            for line in self.lines:
                fh.write(json.dumps(line) + "\n")
        return p


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_audit(self, log):
        return audit.analyse([audit.parse_session(log.write(self.dir))])

    def test_totals_match_raw_usage_and_requests_are_deduplicated(self):
        log = Log().request(usage(read=0, write=10000, out=100), blocks=3) \
                   .request(usage(read=10000, write=500, out=200), blocks=2)
        r = self.run_audit(log)
        self.assertEqual(r["requests"], 2)
        self.assertEqual(r["tokens"], {"input": 0, "cache_read": 10000, "cache_write": 10500, "output": 300})
        # 1h writes cost 2x, reads 0.1x, output 5x
        self.assertAlmostEqual(r["cost_ite"], 10500 * 2 + 10000 * 0.1 + 300 * 5)

    def test_carry_counts_later_requests_and_stops_at_compaction(self):
        log = Log().request(usage(write=5000), tool=("t1", "Read", {"file_path": "/a.py"})) \
                   .result("t1", "x" * 4000) \
                   .request(usage(read=6000)).request(usage(read=6000)) \
                   .compact() \
                   .request(usage(write=2000)).request(usage(read=2000))
        r = self.run_audit(log)
        item = r["top_items"][0]
        self.assertEqual(item["tool"], "Read")
        self.assertEqual(item["size"], 1000)      # 4000 chars / 4
        self.assertEqual(item["resent"], 2)       # not counted after the compaction
        self.assertEqual(r["context"]["compactions"], 1)

    def test_images_count_as_fixed_tokens_not_base64_length(self):
        log = Log().request(usage(write=5000), tool=("t1", "mcp__browser__shot", {"url": "http://x"})) \
                   .result("t1", "", images=2).request(usage(read=9000))
        item = self.run_audit(log)["top_items"][0]
        self.assertEqual(item["size"], 2 * audit.IMAGE_TOKENS)
        self.assertEqual(item["images"], 2)

    def test_cache_miss_after_idle_gap(self):
        log = Log().request(usage(write=50000), ts="2026-10-01T10:00:00Z") \
                   .request(usage(read=50000), ts="2026-10-01T10:01:00Z") \
                   .request(usage(write=51000), ts="2026-10-01T12:30:00Z")
        r = self.run_audit(log)
        self.assertEqual(r["cache_misses"]["count"], 1)
        self.assertIn("idle", r["cache_misses"]["examples"][0]["cause"])

    def test_first_request_after_compaction_is_not_a_miss(self):
        log = Log().request(usage(write=50000)).compact().request(usage(write=40000))
        self.assertEqual(self.run_audit(log)["cache_misses"]["count"], 0)

    def test_re_sent_shares_add_up_to_cache_reads(self):
        log = Log().request(usage(write=8000), tool=("t1", "Bash", {"command": "ls"})).result("t1", "y" * 8000) \
                   .request(usage(read=10000, out=50)).request(usage(read=10000, out=50))
        r = self.run_audit(log)
        rs = r["re_sent"]
        self.assertAlmostEqual(rs["startup_context"] + rs["tool_results"] + rs["conversation"],
                               r["cost_split"]["cache_reads"], places=6)

    def test_subagent_usage_is_included(self):
        log = Log().request(usage(write=1000))
        p = log.write(self.dir)
        sub = os.path.join(self.dir, "s1", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-1.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "assistant", "requestId": "a", "message": {
                "model": "claude-test", "usage": usage(write=1000)}}) + "\n")
        r = audit.analyse([audit.parse_session(p)])
        self.assertEqual(r["subagent_requests"], 1)
        self.assertAlmostEqual(r["cost_split"]["subagents"], 0.5)

    def test_nothing_worth_fixing_verdict(self):
        log = Log().request(usage(write=1000, out=10)).request(usage(read=1000, out=10))
        text = audit.report(self.run_audit(log))
        self.assertIn("nothing worth fixing", text)

    def test_truncated_last_line_is_ignored(self):
        p = Log().request(usage(write=1000)).write(self.dir)
        with open(p, "a") as fh:
            fh.write('{"type": "assistant", "mess')
        self.assertEqual(audit.analyse([audit.parse_session(p)])["requests"], 1)

    def test_project_folder_encoding(self):
        self.assertEqual(audit.encode_project("/Users/a/my.repo"), "-Users-a-my-repo")

    def test_cli_runs_without_saving(self):
        p = Log().request(usage(write=1000)).request(usage(read=1000)).write(self.dir)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = audit.main(["--session", p, "--no-save"])
        self.assertEqual(code, 0)
        self.assertIn("WHERE THE COST GOES", buf.getvalue())


class PromiseTests(unittest.TestCase):
    """The README promises: standard library only, no network."""

    def test_no_network_or_third_party_imports(self):
        with open(AUDIT) as fh:
            tree = ast.parse(fh.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                mods.add((node.module or "").split(".")[0])
        allowed = {"argparse", "datetime", "glob", "json", "os", "re", "sys"}
        self.assertTrue(mods <= allowed, "unexpected imports: %s" % (mods - allowed))

    def test_writes_only_to_its_snapshot_folder(self):
        with open(AUDIT) as fh:
            src = fh.read()
        self.assertEqual(src.count('open(path, "w")'), 1)
        self.assertIn('".claude", "cut-context", "audits"', src)


if __name__ == "__main__":
    unittest.main()
