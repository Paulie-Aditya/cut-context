#!/usr/bin/env python3
"""cut-context audit: where your Claude Code context tokens actually go.

Reads Claude Code's local session logs (~/.claude/projects/<project>/*.jsonl),
weights every request by what it costs, and ranks what keeps getting re-sent:
large tool results (their "carry cost"), the fixed startup context, the
conversation itself, cache misses and repeated file reads. Then it names the
fix for each finding.

Standard library only. It makes no network calls. It reads logs and, unless
--no-save is given, writes one JSON snapshot per run under
~/.claude/cut-context/audits/ so the next run can show what changed.

    python3 audit.py                     # this project, last 7 days
    python3 audit.py --days 30 --top 15
    python3 audit.py --session <file.jsonl>
    python3 audit.py --all               # every project
    python3 audit.py --json              # machine-readable
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

# Cost weights, in "input-token equivalents" (ITE): one uncached input token = 1.
# These ratios hold across current Claude models (cache read 0.1x, 5-minute cache
# write 1.25x, 1-hour cache write 2x, output 5x), so shares are right even when
# dollar prices are unknown.
W_INPUT, W_READ, W_WRITE_5M, W_WRITE_1H, W_OUTPUT = 1.0, 0.1, 1.25, 2.0, 5.0

CHARS_PER_TOKEN = 4.0     # estimate for text in tool results
IMAGE_TOKENS = 1600       # estimate per image block (screenshots)
MISS_MIN_TOKENS = 20000   # a cache write this big on a warm request looks like a miss
LONG_CONTEXT = 200000     # threshold for the long-context finding
NOTHING_WORTH_FIXING = 0.03  # if no finding is worth >= 3% of cost, say so

SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".claude", "cut-context", "audits")


class AuditError(Exception):
    pass


# ---------------------------------------------------------------- finding logs

def projects_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def encode_project(path):
    """Claude Code names a project's log folder after its path, with every
    character that is not a letter or digit replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def find_sessions(root, project=None, all_projects=False, days=7, session=None):
    if session:
        if not os.path.isfile(session):
            raise AuditError("no such session file: %s" % session)
        return [session]
    if all_projects:
        dirs = [d for d in glob.glob(os.path.join(root, "*")) if os.path.isdir(d)]
    else:
        name = project if project and os.sep not in project and not project.startswith(".") \
            else encode_project(project or os.getcwd())
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            raise AuditError("no Claude Code logs for this project (looked in %s). "
                             "Run it from a project you have used Claude Code in, "
                             "or pass --all or --session." % d)
        dirs = [d]
    cutoff = dt.datetime.now().timestamp() - days * 86400 if days else 0
    files = []
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.jsonl")):
            if os.path.getmtime(f) >= cutoff:
                files.append(f)
    if not files:
        raise AuditError("no sessions in the last %d days. Try --days 30 or --all." % days)
    return sorted(files, key=os.path.getmtime)


# ---------------------------------------------------------------- parsing

def _ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def est_tokens(content):
    """Estimate tokens in a tool_result's content (a string or a list of blocks)."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content) / CHARS_PER_TOKEN
    total = 0.0
    for b in content if isinstance(content, list) else [content]:
        if not isinstance(b, dict):
            total += len(str(b)) / CHARS_PER_TOKEN
        elif b.get("type") == "image":
            total += IMAGE_TOKENS
        elif b.get("type") == "text":
            total += len(b.get("text", "")) / CHARS_PER_TOKEN
        else:
            total += len(json.dumps(b)) / CHARS_PER_TOKEN
    return total


def describe_input(name, inp):
    """A short, human label for a tool call: the file, command or URL it touched."""
    if not isinstance(inp, dict):
        return ""
    if isinstance(inp.get("actions"), list):  # batched browser tools
        names, url = [], ""
        for a in inp["actions"]:
            if isinstance(a, dict):
                names.append(str(a.get("name") or (a.get("input") or {}).get("action") or "?"))
                url = url or str((a.get("input") or {}).get("url") or "")
        label = "%d actions (%s)%s" % (len(names), ", ".join(dict.fromkeys(names)), " " + url if url else "")
        return label if len(label) <= 70 else label[:67] + "..."
    for key in ("file_path", "path", "notebook_path", "command", "url", "pattern", "query",
                "description", "prompt"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            v = " ".join(v.split())
            return v if len(v) <= 70 else v[:67] + "..."
    s = json.dumps(inp)
    return s if len(s) <= 70 else s[:67] + "..."


def ite(u):
    """Cost of one request's usage, in input-token equivalents."""
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    write = u.get("cache_creation_input_tokens", 0) or 0
    if w5 is None and w1 is None:
        w5, w1 = write, 0
    w5, w1 = w5 or 0, w1 or 0
    return ((u.get("input_tokens", 0) or 0) * W_INPUT
            + (u.get("cache_read_input_tokens", 0) or 0) * W_READ
            + w5 * W_WRITE_5M + w1 * W_WRITE_1H
            + (u.get("output_tokens", 0) or 0) * W_OUTPUT)


def context_size(u):
    return ((u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0)
            + (u.get("cache_creation_input_tokens", 0) or 0))


def _iter_records(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue  # a half-written last line while a session is live


def parse_session(path):
    """Return the session's timeline: requests, tool results, compactions."""
    events = []          # ("req", rid) | ("result", idx) | ("compact", None)
    requests = {}        # requestId -> dict(usage, model, ts)
    req_order = []
    results = []         # dict(id, size, images)
    calls = {}           # tool_use id -> (name, label, input)
    cost_states = []
    cwd = None
    versions = set()
    for d in _iter_records(path):
        t = d.get("type")
        if d.get("version"):
            versions.add(d["version"])
        if cwd is None and d.get("cwd"):
            cwd = d["cwd"]
        if t == "cost-state":
            cost_states.append(d)
            continue
        if d.get("isSidechain"):
            continue  # older versions kept subagent turns in the main file
        if t == "system" and d.get("subtype") == "compact_boundary":
            events.append(("compact", None))
            continue
        m = d.get("message")
        if not isinstance(m, dict):
            continue
        if t == "assistant":
            rid = d.get("requestId") or m.get("id") or d.get("uuid")
            u = m.get("usage")
            if rid not in requests:
                requests[rid] = {"usage": u or {}, "model": m.get("model"), "ts": _ts(d.get("timestamp"))}
                req_order.append(rid)
                events.append(("req", rid))
            elif u:
                requests[rid]["usage"] = u  # the last record of a request carries its final usage
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    calls[c.get("id")] = (c.get("name") or "?", describe_input(c.get("name"), c.get("input")),
                                          c.get("input") if isinstance(c.get("input"), dict) else {})
        elif t == "user" and isinstance(m.get("content"), list):
            for c in m["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    imgs = sum(1 for b in body if isinstance(b, dict) and b.get("type") == "image") \
                        if isinstance(body, list) else 0
                    results.append({"id": c.get("tool_use_id"), "size": est_tokens(body), "images": imgs})
                    events.append(("result", len(results) - 1))
    reqs = [dict(requests[r], id=r) for r in req_order]
    return {"path": path, "cwd": cwd, "events": events, "requests": reqs, "results": results,
            "calls": calls, "cost_states": cost_states, "versions": sorted(versions)}


def subagent_usage(path):
    """Usage of the subagents a session spawned (stored next to it in <session>/subagents/)."""
    folder = os.path.join(os.path.splitext(path)[0], "subagents")
    total, n, models = 0.0, 0, {}
    for f in glob.glob(os.path.join(folder, "*.jsonl")):
        seen = {}
        for d in _iter_records(f):
            m = d.get("message")
            if d.get("type") == "assistant" and isinstance(m, dict) and m.get("usage"):
                seen[d.get("requestId") or m.get("id") or d.get("uuid")] = (m["usage"], m.get("model"))
        for u, model in seen.values():
            c = ite(u)
            total += c
            n += 1
            models[model] = models.get(model, 0) + c
    return total, n, models


# ---------------------------------------------------------------- analysis

def dollar_rates(cost_states):
    """Dollars per ITE for each model, derived from Claude Code's own cost records
    in the log. Approximate: those records don't split 5-minute from 1-hour cache
    writes, so writes are weighted at 1.25x here."""
    rates = {}
    for cs in cost_states:
        for model, mu in (cs.get("modelUsage") or {}).items():
            cost = mu.get("costUSD") or 0
            units = ((mu.get("inputTokens") or 0) * W_INPUT + (mu.get("cacheReadInputTokens") or 0) * W_READ
                     + (mu.get("cacheCreationInputTokens") or 0) * W_WRITE_5M
                     + (mu.get("outputTokens") or 0) * W_OUTPUT)
            if cost > 0 and units > 0:
                rates[model] = cost / units  # later records are cumulative; keep the last
    return rates


def analyse(sessions, top=10):
    totals = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    cost = 0.0
    cost_by_model = {}
    sub_cost, sub_reqs, sub_models = 0.0, 0, {}
    n_req = 0
    ctx_sizes = []
    carry_items = []
    by_tool = {}
    base_carry = 0.0       # cache-read tokens spent re-sending the startup context
    result_carry = 0.0     # cache-read tokens spent re-sending tool results
    cache_read_total = 0.0
    misses = []
    reads = {}
    compactions = 0
    long_excess = 0.0
    rates = {}
    cwds = set()
    versions = set()

    for s in sessions:
        cwds.add(s["cwd"])
        versions.update(s["versions"])
        rates.update(dollar_rates(s["cost_states"]))
        reqs = {r["id"]: r for r in s["requests"]}
        ev = s["events"]
        # requests after each event index, until the next compaction
        after = [0] * (len(ev) + 1)
        count = 0
        for i in range(len(ev) - 1, -1, -1):
            k = ev[i][0]
            if k == "compact":
                count = 0
            elif k == "req":
                count += 1
            after[i] = count
        compactions += sum(1 for k, _ in ev if k == "compact")

        prev_ts, fresh = None, True     # fresh: first request of a session or after compaction
        base = None
        for i, (k, v) in enumerate(ev):
            if k == "compact":
                fresh = True
                continue
            if k == "result":
                r = s["results"][v]
                name, label, inp = s["calls"].get(r["id"], ("?", "", {}))
                n_after = after[i + 1] if i + 1 < len(after) else 0
                carried = r["size"] * n_after
                result_carry += carried
                by_tool[name] = by_tool.get(name, 0) + carried
                carry_items.append({"tool": name, "label": label, "size": round(r["size"]),
                                    "resent": n_after, "carried": carried, "images": r["images"],
                                    "session": os.path.basename(s["path"])})
                if name == "Read" and inp.get("file_path"):
                    key = inp["file_path"]
                    e = reads.setdefault(key, {"count": 0, "tokens": 0.0})
                    e["count"] += 1
                    e["tokens"] += r["size"]
                continue
            r = reqs[v]
            u = r["usage"]
            if not u:
                continue
            n_req += 1
            c = ite(u)
            cost += c
            cost_by_model[r["model"]] = cost_by_model.get(r["model"], 0) + c
            totals["input"] += u.get("input_tokens", 0) or 0
            totals["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            totals["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            totals["output"] += u.get("output_tokens", 0) or 0
            cache_read_total += u.get("cache_read_input_tokens", 0) or 0
            size = context_size(u)
            ctx_sizes.append(size)
            long_excess += max(0, size - LONG_CONTEXT)
            if base is None:
                base = size
            else:
                base_carry += min(base, u.get("cache_read_input_tokens", 0) or 0)
            write = u.get("cache_creation_input_tokens", 0) or 0
            if not fresh and write >= MISS_MIN_TOKENS and write >= 0.5 * size:
                gap = (r["ts"] - prev_ts) if (r["ts"] and prev_ts) else None
                cc = u.get("cache_creation") or {}
                ttl = 3600 if (cc.get("ephemeral_1h_input_tokens") or 0) >= (cc.get("ephemeral_5m_input_tokens") or 0) else 300
                cause = ("idle %s, longer than the %s cache lifetime" % (_dur(gap), "1-hour" if ttl == 3600 else "5-minute")
                         if gap is not None and gap > ttl else
                         "context changed (a model or effort switch, an edited CLAUDE.md, tools added or removed)")
                misses.append({"tokens": write, "extra": write * ((W_WRITE_1H if ttl == 3600 else W_WRITE_5M) - W_READ),
                               "cause": cause, "when": r["ts"], "session": os.path.basename(s["path"])})
            fresh = False
            if r["ts"]:
                prev_ts = r["ts"]

        sc, sn, sm = subagent_usage(s["path"])
        sub_cost += sc
        sub_reqs += sn
        for mdl, v in sm.items():
            sub_models[mdl] = sub_models.get(mdl, 0) + v

    if n_req == 0:
        raise AuditError("these sessions contain no API requests with token usage.")

    total_cost = cost + sub_cost
    convo_carry = max(0.0, cache_read_total - base_carry - result_carry)
    carry_items.sort(key=lambda x: x["carried"], reverse=True)

    def share(read_tokens):
        return read_tokens * W_READ / total_cost if total_cost else 0.0

    for it in carry_items:
        it["share"] = share(it["carried"])

    dollars = None
    if rates:
        dollars = 0.0
        for mdl, c in list(cost_by_model.items()) + list(sub_models.items()):
            rate = rates.get(mdl) or (sum(rates.values()) / len(rates))
            dollars += c * rate
    usd_per_ite = (dollars / total_cost) if dollars and total_cost else None

    repeated = sorted(((p, e) for p, e in reads.items() if e["count"] > 1),
                      key=lambda x: x[1]["tokens"] * x[1]["count"], reverse=True)

    res = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "sessions": [os.path.basename(s["path"]) for s in sessions],
        "projects": sorted(c for c in cwds if c),
        "claude_code_versions": sorted(versions)[-3:],
        "requests": n_req,
        "subagent_requests": sub_reqs,
        "tokens": totals,
        "cost_ite": total_cost,
        "cost_usd_estimate": dollars,
        "cost_split": {
            "cache_reads": totals["cache_read"] * W_READ / total_cost,
            "cache_writes": (cost - totals["input"] * W_INPUT - totals["cache_read"] * W_READ
                             - totals["output"] * W_OUTPUT) / total_cost,
            "output": totals["output"] * W_OUTPUT / total_cost,
            "input": totals["input"] * W_INPUT / total_cost,
            "subagents": sub_cost / total_cost,
        },
        "context": {
            "avg": sum(ctx_sizes) / len(ctx_sizes),
            "max": max(ctx_sizes),
            "over_200k_share": sum(1 for x in ctx_sizes if x > LONG_CONTEXT) / len(ctx_sizes),
            "compactions": compactions,
        },
        "re_sent": {
            "startup_context": share(base_carry),
            "tool_results": share(result_carry),
            "conversation": share(convo_carry),
        },
        "carry_by_tool": sorted(((k, share(v)) for k, v in by_tool.items()), key=lambda x: -x[1]),
        "top_items": carry_items[:top],
        "cache_misses": {
            "count": len(misses),
            "share": sum(m["extra"] for m in misses) / total_cost if total_cost else 0,
            "examples": sorted(misses, key=lambda m: -m["extra"])[:3],
        },
        "repeated_reads": [{"file": p, "count": e["count"], "tokens": round(e["tokens"])}
                           for p, e in repeated[:5]],
        "long_context_share": share(long_excess),
        "per_request": {"cost_ite": total_cost / n_req, "context": sum(ctx_sizes) / len(ctx_sizes)},
        "usd_per_ite": usd_per_ite,
    }
    res["claude_md"] = claude_md_files(res["projects"])
    res["findings"] = findings(res)
    return res


def _dur(sec):
    if sec is None:
        return "?"
    if sec >= 3600:
        return "%.1fh" % (sec / 3600)
    return "%dm" % round(sec / 60)


def claude_md_files(projects):
    out = []
    paths = [os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")]
    for p in projects:
        paths += [os.path.join(p, "CLAUDE.md"), os.path.join(p, ".claude", "CLAUDE.md")]
    for p in dict.fromkeys(paths):
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            out.append({"path": p, "lines": text.count("\n") + 1, "tokens": round(len(text) / CHARS_PER_TOKEN)})
    return out


def findings(r):
    """Turn the numbers into ranked findings, each with a fix. Shares are of total cost."""
    F = []
    tools = dict(r["carry_by_tool"])
    browser = sum(v for k, v in tools.items() if "browser" in k.lower() or "chrome" in k.lower()
                  or "playwright" in k.lower() or "puppeteer" in k.lower())
    mcp_other = sum(v for k, v in tools.items() if k.startswith("mcp__")) - browser
    if browser > 0:
        F.append({"share": browser, "title": "Browser tool results (screenshots, page dumps) stay in context",
                  "fix": "Run browser checks in a subagent so the screenshots stay in its context and only a "
                         "summary returns. Prefer text snapshots (read_page/get_page_text) over screenshots."})
    if mcp_other > 0.01:
        F.append({"share": mcp_other, "title": "Large MCP tool results stay in context",
                  "fix": "Ask for narrower results (filters, limits), or run the MCP-heavy step in a subagent."})
    if tools.get("Read", 0) > 0.01:
        F.append({"share": tools["Read"], "title": "Whole-file reads stay in context",
                  "fix": "Point Claude at the lines it needs (Grep first, then Read with offset/limit). For big "
                         "repos, a code index or a large-file read gate (see the README) avoids whole-file reads."})
    if tools.get("Bash", 0) > 0.01:
        F.append({"share": tools["Bash"], "title": "Long command output stays in context",
                  "fix": "Filter output before Claude sees it (tail, grep for failures), or a hook that does it "
                         "for test runners (see the Claude Code docs' cost page)."})
    if r["long_context_share"] > 0.01:
        F.append({"share": r["long_context_share"],
                  "title": "Context above 200k tokens on %.0f%% of requests (avg %s, max %s)" % (
                      r["context"]["over_200k_share"] * 100, _k(r["context"]["avg"]), _k(r["context"]["max"])),
                  "fix": "/clear between unrelated tasks (it is free), or /compact earlier with instructions on "
                         "what to keep. Upper bound: the share of cost spent on context above 200k."})
    if r["cache_misses"]["share"] > 0.005:
        F.append({"share": r["cache_misses"]["share"],
                  "title": "%d cache misses re-wrote the whole context" % r["cache_misses"]["count"],
                  "fix": "Most follow idle gaps longer than the cache lifetime or mid-session model/effort "
                         "switches. Start fresh (/clear) after a long break instead of continuing a huge session."})
    if r["cost_split"]["subagents"] > 0.10:
        F.append({"share": r["cost_split"]["subagents"],
                  "title": "Subagents: %d requests" % r["subagent_requests"],
                  "fix": "Each subagent starts with its own context. Give simple ones a smaller model "
                         "(model: haiku in the agent definition), keep spawn prompts short, and run fewer "
                         "in parallel when one would do."})
    for c in r["claude_md"]:
        if c["lines"] > 200:
            share = c["tokens"] * r["requests"] * W_READ / r["cost_ite"] if r["cost_ite"] else 0
            F.append({"share": share, "title": "%s is %d lines (~%s tokens on every request)" % (
                          _short(c["path"]), c["lines"], _k(c["tokens"])),
                      "fix": "Move workflow-specific sections into skills, which load only when used. "
                             "The docs suggest keeping CLAUDE.md under 200 lines."})
    if r["repeated_reads"]:
        top = r["repeated_reads"][0]
        F.append({"share": 0.0, "title": "%d files read more than once (top: %s, %d times)" % (
                      len(r["repeated_reads"]), _short(top["file"]), top["count"]),
                  "fix": "Usually harmless; if a big file is re-read after edits, read only the changed range."})
    F.sort(key=lambda f: -f["share"])
    return F


def _k(n):
    return "%.0fk" % (n / 1000) if n >= 1000 else "%d" % n


def _short(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home) else p


# ---------------------------------------------------------------- snapshots

def snapshot_key(r):
    return re.sub(r"[^A-Za-z0-9]+", "-", "|".join(r["projects"]) or "all").strip("-")[-80:]


def previous_snapshot(r):
    files = sorted(glob.glob(os.path.join(SNAPSHOT_DIR, snapshot_key(r) + "--*.json")))
    if not files:
        return None
    try:
        with open(files[-1]) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_snapshot(r):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(SNAPSHOT_DIR, "%s--%s.json" % (snapshot_key(r), stamp))
    keep = {k: r[k] for k in ("generated", "projects", "requests", "cost_ite", "cost_split",
                              "context", "re_sent", "per_request", "sessions")}
    with open(path, "w") as fh:
        json.dump(keep, fh, indent=1)
    return path


# ---------------------------------------------------------------- report

def pct(x):
    return "%.0f%%" % (x * 100) if x >= 0.0095 else "<1%"


def report(r, prev=None):
    L = []
    usd = r["cost_usd_estimate"]
    L.append("cut-context audit · %d session%s · %d requests (+%d in subagents) · %s" % (
        len(r["sessions"]), "" if len(r["sessions"]) == 1 else "s", r["requests"], r["subagent_requests"],
        ", ".join(_short(p) for p in r["projects"]) or "?"))
    L.append("")
    top = r["findings"][0] if r["findings"] else None
    if not top or top["share"] < NOTHING_WORTH_FIXING:
        L.append("VERDICT: nothing worth fixing. No single leak is worth 3% of your cost in these sessions.")
    else:
        L.append("VERDICT: biggest leak is %s of your cost: %s." % (pct(top["share"]), top["title"].lower()))
    L.append("")
    s = r["cost_split"]
    L.append("WHERE THE COST GOES" + (" (≈$%.2f, from Claude Code's own cost records)" % usd if usd else ""))
    L.append("  context re-read from cache  %5s" % pct(s["cache_reads"]))
    L.append("  cache writes                %5s" % pct(s["cache_writes"]))
    L.append("  Claude's output + thinking  %5s" % pct(s["output"]))
    L.append("  subagents                   %5s" % pct(s["subagents"]))
    c = r["context"]
    L.append("  context per request: avg %s, max %s, over 200k on %s of requests, %d compaction%s" % (
        _k(c["avg"]), _k(c["max"]), pct(c["over_200k_share"]), c["compactions"], "" if c["compactions"] == 1 else "s"))
    L.append("")
    rs = r["re_sent"]
    L.append("WHAT GETS RE-SENT (share of total cost)")
    L.append("  the conversation itself      %5s   (your messages, Claude's replies, history)" % pct(rs["conversation"]))
    L.append("  tool results                 %5s" % pct(rs["tool_results"]))
    L.append("  startup context              %5s   (system prompt, tools, CLAUDE.md, skills list)" % pct(rs["startup_context"]))
    L.append("")
    if r["top_items"]:
        L.append("TOP CARRY COST: tool results re-sent on later requests (estimated sizes)")
        for i, it in enumerate(r["top_items"], 1):
            img = " [%d image%s]" % (it["images"], "" if it["images"] == 1 else "s") if it["images"] else ""
            L.append("  %2d. %5s  %s × %d requests  %s %s%s" % (
                i, pct(it["share"]), _k(it["size"]), it["resent"], it["tool"], it["label"], img))
        L.append("")
    if r["findings"]:
        L.append("FINDINGS AND FIXES (ranked by share of cost; they overlap, so don't add them up;")
        L.append("fixes are suggestions, nothing has been changed)")
        for i, f in enumerate(r["findings"], 1):
            L.append("  %d. [%s] %s" % (i, pct(f["share"]) if f["share"] else "–", f["title"]))
            L.append("     fix: %s" % f["fix"])
        L.append("")
    if prev:
        a, b = prev["per_request"], r["per_request"]
        L.append("SINCE YOUR LAST AUDIT (%s), per request, so a lighter week doesn't count as a saving:" % prev["generated"])
        L.append("  context  %s → %s (%+.0f%%)" % (_k(a["context"]), _k(b["context"]), _chg(a["context"], b["context"])))
        L.append("  cost     %.0f → %.0f input-token equivalents (%+.0f%%)" % (a["cost_ite"], b["cost_ite"],
                                                                             _chg(a["cost_ite"], b["cost_ite"])))
        L.append("")
    L.append("Notes: costs are weighted (cache read 0.1x, cache write 1.25x/2x, output 5x input). Tool-result")
    L.append("sizes are estimates (about 4 characters per token, about %d tokens per image); token totals" % IMAGE_TOKENS)
    L.append("come from the usage fields in your logs, the same ones ccusage reads.")
    return "\n".join(L)


def _chg(a, b):
    return (b - a) / a * 100 if a else 0.0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Where your Claude Code context tokens go. Reads local logs; no network.")
    ap.add_argument("--project", help="project path (default: current directory)")
    ap.add_argument("--all", action="store_true", help="every project")
    ap.add_argument("--session", help="one session .jsonl file")
    ap.add_argument("--days", type=int, default=7, help="sessions modified in the last N days (default 7; 0 = all)")
    ap.add_argument("--top", type=int, default=10, help="how many carry items to list")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--no-save", action="store_true", help="don't write a snapshot for the next comparison")
    ap.add_argument("--root", default=projects_dir(), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        files = find_sessions(a.root, a.project, a.all, a.days, a.session)
        res = analyse([parse_session(f) for f in files], top=a.top)
    except AuditError as e:
        print("cut-context: %s" % e, file=sys.stderr)
        return 2
    prev = None if a.no_save else previous_snapshot(res)
    if a.json:
        print(json.dumps(res, indent=1, default=str))
    else:
        print(report(res, prev))
    if not a.no_save:
        path = save_snapshot(res)
        if not a.json:
            print("\nSaved snapshot for next time: %s" % _short(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
