"""cut-context benchmark: the same tasks with and without a fix the audit
recommends, run through Claude Code headless, measured from Claude Code's own
usage report.

    python3 bench/run.py --reps 3 --model haiku
    python3 bench/run.py --only noisy-output --reps 1     # one scenario

Each run gets a fresh copy of the fixture project (bench/fixture.py), runs
Claude Code with your user settings, MCP servers and skills switched off, and
checks every task's result. Results go to bench/results/.

This spends real tokens on your account (about $0.10-0.50 per run on Haiku).
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixture  # noqa: E402

RESULTS = os.path.join(HERE, "results")

FIX_TESTS = "Run `python3 -m unittest discover -v` and fix the failing test by fixing the code, not the test."


def _num(name):
    return "What value does %s() in inventory.py return? Reply with just the number." % name


def check_answer(expected):
    return lambda d, out: str(expected) in out


def check_tests(d, out):
    r = subprocess.run([sys.executable, "-m", "unittest", "discover"], cwd=d, capture_output=True, text=True)
    return r.returncode == 0


def check_slugify(d, out):
    r = subprocess.run([sys.executable, "-c", "import utils; assert utils.slugify('Hello,  World!') == 'hello-world'"],
                       cwd=d, capture_output=True, text=True)
    return r.returncode == 0


def check_readme(d, out):
    with open(os.path.join(d, "README.md")) as fh:
        return "## Setup" in fh.read()


TAIL_HOOK = '''import json, sys
data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "")
if "unittest" in cmd or "pytest" in cmd:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
        "updatedInput": dict(data["tool_input"], command="(%s) 2>&1 | tail -40" % cmd)}}))
'''

READ_GUIDE = ("# Reading code\n\nFor files over 300 lines, find the symbol with Grep first, then Read only "
              "the lines you need with offset/limit. Don't read whole large files.\n")

SCENARIOS = {
    "long-session": {
        "title": "Six unrelated tasks: one long session vs /clear between tasks",
        "arms": {"without": "one session", "with": "/clear between tasks"},
        "tasks": [
            ("Add a function slugify(text) to utils.py: lowercase it, replace each run of characters that are "
             "not letters or digits with a single '-', and strip '-' from both ends.", check_slugify),
            (_num("restock_threshold"), check_answer(fixture.ANSWERS["restock_threshold"])),
            (FIX_TESTS, check_tests),
            ("Add a '## Setup' section to README.md that says to run the tests with python3 -m unittest.",
             check_readme),
            (_num("warehouse_capacity"), check_answer(fixture.ANSWERS["warehouse_capacity"])),
            ("How many data rows does orders.csv have, not counting the header? Reply with just the number.",
             check_answer(400)),
        ],
        # without: every task continues the same session; with: every task starts fresh
        "fresh_each_task": {"without": False, "with": True},
    },
    "after-big-task": {
        "title": "One heavy task, then five small ones: keep going vs /clear after the heavy one",
        "arms": {"without": "keep the session", "with": "/clear after the heavy task"},
        "tasks": [
            ("Read all of inventory.py and orders.csv, then summarise in five bullets what kinds of pricing rules "
             "exist and how the orders are distributed across SKUs.", lambda d, out: len(out) > 200),
            ("Add a function slugify(text) to utils.py: lowercase it, replace each run of characters that are "
             "not letters or digits with a single '-', and strip '-' from both ends.", check_slugify),
            (_num("restock_threshold"), check_answer(fixture.ANSWERS["restock_threshold"])),
            ("Add a '## Setup' section to README.md that says to run the tests with python3 -m unittest.",
             check_readme),
            (_num("warehouse_capacity"), check_answer(fixture.ANSWERS["warehouse_capacity"])),
            (_num("late_fee_rate"), check_answer(fixture.ANSWERS["late_fee_rate"])),
        ],
        # with: the five small tasks run in one session that starts after the heavy task
        "clear_after_first": {"with": True},
    },
    "big-file": {
        "title": "Three questions about a 2,700-line file, in one session",
        "arms": {"without": "no guidance", "with": "CLAUDE.md: grep, then read ranges"},
        "tasks": [(_num(n), check_answer(fixture.ANSWERS[n]))
                  for n in ("warehouse_capacity", "late_fee_rate", "max_batch_size")],
        "setup": {"with": {"CLAUDE.md": READ_GUIDE}},
    },
    "noisy-output": {
        "title": "Fix a failing test in a 1,200-test suite, then two follow-ups",
        "arms": {"without": "full test output", "with": "hook: keep the last 40 lines"},
        "tasks": [
            (FIX_TESTS, check_tests),
            (_num("max_batch_size"), check_answer(fixture.ANSWERS["max_batch_size"])),
            ("How many data rows does orders.csv have, not counting the header? Reply with just the number.",
             check_answer(400)),
        ],
        "setup": {"with": {
            ".claude/hooks/tail_tests.py": TAIL_HOOK,
            ".claude/settings.json": json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": "python3 .claude/hooks/tail_tests.py"}]}]}}, indent=1),
        }},
    },
}


def claude(prompt, cwd, model, resume=None, budget=1.0):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model,
           "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
           "--permission-mode", "dontAsk", "--allowedTools", "Read Edit Write Grep Glob Bash",
           "--max-budget-usd", str(budget)]
    if resume:
        cmd += ["--resume", resume]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {"is_error": True, "result": (r.stdout + r.stderr)[-2000:]}
    out["_seconds"] = round(time.time() - t0, 1)
    return out


def step_costs(steps):
    """total_cost_usd is a running total for the session, so a resumed session reports
    everything spent so far. A step's own cost is the change since the session's last step."""
    last, out = {}, []
    for st in steps:
        total = st.get("cost_usd") or 0
        sid = st.get("session_id")
        out.append(max(0.0, total - last.get(sid, 0.0)))
        last[sid] = total
    return out


def run_one(scenario, arm, rep, model, keep):
    sc = SCENARIOS[scenario]
    d = tempfile.mkdtemp(prefix="cutctx-%s-%s-%d-" % (scenario, arm, rep))
    d = os.path.realpath(d)
    fixture.build(d)
    for rel, text in sc.get("setup", {}).get(arm, {}).items():
        os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
        with open(os.path.join(d, rel), "w") as fh:
            fh.write(text)
    fresh = sc.get("fresh_each_task", {}).get(arm, False)
    clear_after_first = sc.get("clear_after_first", {}).get(arm, False)
    steps, sid = [], None
    for i, (prompt, check) in enumerate(sc["tasks"]):
        if clear_after_first and i == 1:
            sid = None
        out = claude(prompt, d, model, resume=None if fresh else sid)
        sid = out.get("session_id") or sid
        u = out.get("usage") or {}
        steps.append({"prompt": prompt[:80], "passed": bool(check(d, out.get("result") or "")),
                      "error": bool(out.get("is_error")), "usage": u, "cost_usd": out.get("total_cost_usd"),
                      "turns": out.get("num_turns"), "seconds": out["_seconds"], "session_id": out.get("session_id")})
    res = {"scenario": scenario, "arm": arm, "rep": rep, "model": model, "dir": d,
           "finished": dt.datetime.now().isoformat(timespec="seconds"), "steps": steps}
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "%s--%s--%d.json" % (scenario, arm, rep)), "w") as fh:
        json.dump(res, fh, indent=1)
    if not keep:
        shutil.rmtree(d, ignore_errors=True)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--only", choices=sorted(SCENARIOS))
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--keep", action="store_true", help="keep the run folders")
    a = ap.parse_args(argv)
    if not shutil.which("claude"):
        print("claude CLI not found on PATH", file=sys.stderr)
        return 2
    jobs = [(s, arm, rep) for rep in range(1, a.reps + 1)
            for s in ([a.only] if a.only else SCENARIOS) for arm in ("without", "with")]
    with cf.ThreadPoolExecutor(a.parallel) as ex:
        futs = {ex.submit(run_one, s, arm, rep, a.model, a.keep): (s, arm, rep) for s, arm, rep in jobs}
        for f in cf.as_completed(futs):
            s, arm, rep = futs[f]
            try:
                r = f.result()
                cost = sum(step_costs(r["steps"]))
                ok = sum(st["passed"] for st in r["steps"])
                print("%-13s %-8s rep %d: $%.3f, %d/%d tasks passed" % (s, arm, rep, cost, ok, len(r["steps"])),
                      flush=True)
            except Exception as e:  # keep the other runs going
                print("%-13s %-8s rep %d: FAILED %r" % (s, arm, rep, e), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
