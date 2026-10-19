"""Turns bench/results/*.json into bench/RESULTS.md and SVG charts in docs/.

    python3 bench/report.py

Charts are plain SVG (no dependencies) with light and dark colors, so they
render in a GitHub README in either theme.
"""

import glob
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "cut-context", "skills", "audit"))
import audit  # noqa: E402
from run import SCENARIOS, step_costs  # noqa: E402

# Validated categorical slots 1-2 (dataviz reference palette), light and dark.
STYLE = """
<style>
  .surface { fill: #fcfcfb; } .t1 { fill: #0b0b0b; } .t2 { fill: #52514e; } .grid { stroke: #e4e3df; }
  .s1 { fill: #eb6834; } .s2 { fill: #2a78d6; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; } .t1 { fill: #f0efec; } .t2 { fill: #c3c2b7; } .grid { stroke: #383835; }
    .s1 { fill: #d95926; } .s2 { fill: #3987e5; }
  }
  text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
</style>"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bar(x, y, w, h, cls, r=4):
    """Horizontal bar: square at the baseline (left), 4px rounded data end (right)."""
    if w <= 0:
        return ""
    r = min(r, w, h / 2)
    return ('<path class="%s" d="M%.1f,%.1f h%.1f a%.1f,%.1f 0 0 1 %.1f,%.1f v%.1f a%.1f,%.1f 0 0 1 %.1f,%.1f h%.1f z"/>'
            % (cls, x, y, w - r, r, r, r, r, h - 2 * r, r, r, -r, r, -(w - r)))


def hbar_chart(path, title, subtitle, groups, series, fmt, note=""):
    """groups: [(label, [v_series1, v_series2, ...], extra_text)]; series: [(name, css_class)]"""
    W, left, right, top = 760, 230, 150, 92
    bh, gap, group_gap = 18, 2, 22
    n = len(series)
    gh = n * bh + (n - 1) * gap
    H = top + len(groups) * (gh + group_gap) + (40 if note else 20)
    vmax = max(max(v for v in vals if v is not None) for _, vals, _ in groups) or 1
    scale = (W - left - right) / vmax
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" role="img" aria-labelledby="t d">'
         % (W, H, W), STYLE,
         '<title id="t">%s</title><desc id="d">%s</desc>' % (esc(title), esc(subtitle)),
         '<rect class="surface" width="%d" height="%d" rx="8"/>' % (W, H),
         '<text class="t1" x="20" y="32" font-size="17" font-weight="600">%s</text>' % esc(title),
         '<text class="t2" x="20" y="54" font-size="13">%s</text>' % esc(subtitle)]
    lx = 20
    for name, cls in (series if n > 1 else []):  # legend for two or more series; one series needs none
        o.append('<rect class="%s" x="%d" y="66" width="12" height="12" rx="2"/>' % (cls, lx))
        o.append('<text class="t2" x="%d" y="76" font-size="12">%s</text>' % (lx + 18, esc(name)))
        lx += 26 + 7 * len(name)
    y = top
    o.append('<line class="grid" x1="%d" y1="%d" x2="%d" y2="%d" stroke-width="1"/>'
             % (left, top - 6, left, top + len(groups) * (gh + group_gap) - group_gap + 6))
    for label, vals, extra in groups:
        o.append('<text class="t1" x="%d" y="%.1f" font-size="13" text-anchor="end">%s</text>'
                 % (left - 12, y + gh / 2 - (6 if extra else -4), esc(label)))
        if extra:
            o.append('<text class="t2" x="%d" y="%.1f" font-size="12" text-anchor="end">%s</text>'
                     % (left - 12, y + gh / 2 + 12, esc(extra)))
        for i, ((name, cls), v) in enumerate(zip(series, vals)):
            by = y + i * (bh + gap)
            w = (v or 0) * scale
            o.append('<g><title>%s: %s</title>%s</g>' % (esc(name), esc(fmt(v)), bar(left, by, w, bh, cls)))
            o.append('<text class="t1" x="%.1f" y="%.1f" font-size="12">%s</text>' % (left + w + 6, by + 13, esc(fmt(v))))
        y += gh + group_gap
    if note:
        o.append('<text class="t2" x="20" y="%d" font-size="11">%s</text>' % (H - 14, esc(note)))
    o.append("</svg>")
    with open(path, "w") as fh:
        fh.write("\n".join(o))


def load():
    runs = {}
    for f in glob.glob(os.path.join(HERE, "results", "*.json")):
        with open(f) as fh:
            r = json.load(fh)
        runs.setdefault((r["scenario"], r["arm"]), []).append(r)
    return runs


def summarise(rs):
    def per_run(r):
        tokens = sum(audit.context_size(s["usage"]) + (s["usage"].get("output_tokens") or 0) for s in r["steps"])
        return {"usd": sum(step_costs(r["steps"])),
                "ite": sum(audit.ite(s["usage"]) for s in r["steps"]),
                "tokens": tokens,
                "passed": sum(s["passed"] for s in r["steps"]), "tasks": len(r["steps"]),
                "per_task_ctx": [audit.context_size(s["usage"]) for s in r["steps"]]}
    xs = [per_run(r) for r in rs]
    m = lambda k: st.mean(x[k] for x in xs)  # noqa: E731
    return {"n": len(xs), "usd": m("usd"), "ite": m("ite"), "tokens": m("tokens"),
            "usd_min": min(x["usd"] for x in xs), "usd_max": max(x["usd"] for x in xs),
            "passed": sum(x["passed"] for x in xs), "tasks": sum(x["tasks"] for x in xs),
            "per_task_ctx": [st.mean(col) for col in zip(*[x["per_task_ctx"] for x in xs])],
            "model": rs[0]["model"]}


def claude_version():
    import subprocess
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.split()[0]
    except (OSError, IndexError):
        return ""


def real_world(session, out):
    """Where the cost goes in one real session, from the audit itself."""
    r = audit.analyse([audit.parse_session(session)])
    sp, rs = r["cost_split"], r["re_sent"]
    groups = [("Re-sent: the conversation", [rs["conversation"] * 100], ""),
              ("Re-sent: tool results", [rs["tool_results"] * 100], ""),
              ("Re-sent: startup context", [rs["startup_context"] * 100], ""),
              ("Cache writes", [sp["cache_writes"] * 100], ""),
              ("Claude's replies + thinking", [sp["output"] * 100], "what terse-reply tools cut")]
    hbar_chart(out, "Where one real Claude Code session's cost went",
               "The author's heaviest session: %d requests, average context %s tokens per request."
               % (r["requests"], k(r["context"]["avg"])),
               groups, [("share of cost", "s2")], lambda v: "%.0f%%" % v,
               note="From /cut-context:audit on the author's own logs. Re-sent = context Claude re-reads from cache.")
    return r


def k(n):
    return "%.0fk" % (n / 1000) if n >= 1000 else "%d" % n


def main():
    runs = load()
    if not runs:
        print("no results; run bench/run.py first")
        return 1
    os.makedirs(DOCS, exist_ok=True)
    S = {key: summarise(v) for key, v in runs.items()}
    order = [s for s in SCENARIOS if (s, "without") in S and (s, "with") in S]
    model = S[(order[0], "without")]["model"]
    reps = min(S[(s, a)]["n"] for s in order for a in ("without", "with"))

    # 1. cost per run, without vs with
    groups = []
    for s in order:
        a, b = S[(s, "without")], S[(s, "with")]
        chg = (b["usd"] - a["usd"]) / a["usd"] * 100
        groups.append((SCENARIOS[s]["arms"]["with"], [a["usd"] * 100, b["usd"] * 100],
                       "%s%.0f%% cost" % ("+" if chg > 0 else "", chg)))
    hbar_chart(os.path.join(DOCS, "bench-cost.svg"),
               "Cost of the same tasks, without and with the fix",
               "Claude Code (%s), mean of %d runs each. Every task checked for pass/fail." % (model, reps),
               groups, [("without the fix", "s1"), ("with the fix", "s2")],
               lambda v: "%.2f¢" % v,
               note="US cents per run, from Claude Code's own cost report. Tiny because Haiku is cheap; the ratios are the point.")

    # 2. long session: context sent per task
    if ("after-big-task", "without") in S:
        a, b = S[("after-big-task", "without")], S[("after-big-task", "with")]
        tasks = ["read the big file + CSV", "add slugify()", "look up a value", "README section",
                 "look up a value", "look up a value"]
        groups = [("Task %d" % (i + 1), [x, y], tasks[i] if i < len(tasks) else "")
                  for i, (x, y) in enumerate(zip(a["per_task_ctx"], b["per_task_ctx"]))]
        hbar_chart(os.path.join(DOCS, "bench-after-big-task.svg"),
                   "Tokens Claude processes per task, after one heavy task",
                   "Keep going in the same session vs /clear after task 1 (%s, mean of %d runs)." % (model, reps),
                   groups, [("keep the session", "s1"), ("/clear after task 1", "s2")],
                   lambda v: k(v),
                   note="Input + cache read + cache write tokens across all requests in the task. Task 1 is the same prompt in both arms; the gap there is run-to-run variation.")

    # 3. the real-world view, if a session log is given
    if len(sys.argv) > 1:
        real_world(sys.argv[1], os.path.join(DOCS, "real-world.svg"))

    # 4. RESULTS.md
    L = ["# Benchmark results", "",
         "Claude Code %s, `--model %s`, %d runs per arm. Each run uses a fresh copy of the fixture project "
         "(`bench/fixture.py`) with user settings, MCP servers and skills off. Tokens and dollars come from "
         "Claude Code's own `--output-format json` usage report." % (
             claude_version(), model, reps), "",
         "| Scenario | Fix | ¢ per run without → with | Change | Weighted tokens without → with | Tasks passed without · with |",
         "|---|---|---|---:|---|---|"]
    for s in order:
        a, b = S[(s, "without")], S[(s, "with")]
        L.append("| %s | %s | %.2f¢ → %.2f¢ | **%+.0f%%** | %s → %s | %d/%d · %d/%d |" % (
            SCENARIOS[s]["title"], SCENARIOS[s]["arms"]["with"], a["usd"] * 100, b["usd"] * 100,
            (b["usd"] - a["usd"]) / a["usd"] * 100, k(a["ite"]), k(b["ite"]), a["passed"], a["tasks"],
            b["passed"], b["tasks"]))
    L += ["", "Cost is in input-token equivalents (ITE): cache read 0.1x, 1-hour cache write 2x, output 5x. "
          "Dollar ranges per run: " + "; ".join(
              "%s %s $%.4f–$%.4f" % (s, arm, S[(s, arm)]["usd_min"], S[(s, arm)]["usd_max"])
              for s in order for arm in ("without", "with")) + ".", ""]
    for sc, h1, h2 in (("after-big-task", "Keep the session", "/clear after task 1"),
                       ("long-session", "One long session", "/clear between tasks")):
        if (sc, "without") not in S:
            continue
        a, b = S[(sc, "without")], S[(sc, "with")]
        L += ["", "## Tokens processed per task (%s)" % sc, "",
              "| Task | %s | %s |" % (h1, h2), "|---|---:|---:|"]
        for i, (x, y) in enumerate(zip(a["per_task_ctx"], b["per_task_ctx"])):
            L.append("| %d | %s | %s |" % (i + 1, k(x), k(y)))
    if ("after-big-task", "without") in S:
        fu = {arm: st.mean(sum(step_costs(r["steps"])[1:]) for r in runs[("after-big-task", arm)])
              for arm in ("without", "with")}
        L += ["", "**Follow-up tasks only (after-big-task, tasks 2-6):** %.2f¢ → %.2f¢ per run (%+.0f%%)." % (
            fu["without"] * 100, fu["with"] * 100, (fu["with"] - fu["without"]) / fu["without"] * 100)]
    with open(os.path.join(HERE, "RESULTS.md"), "w") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
