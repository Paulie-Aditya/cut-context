---
name: audit
description: >-
  Audits where your Claude Code tokens actually go, from your local session logs.
  Weights every request by cost and ranks what keeps getting re-sent to Claude:
  the conversation itself, large tool results (their carry cost), the startup
  context, cache misses, long context and subagents. Each finding comes with the
  exact fix. Read-only: no hooks, no network. Use when the user says "where do my
  tokens go", "why do I hit my limit", "audit my usage", "context audit", "token
  leak", "why is Claude Code so expensive", or "cut context".
argument-hint: "[--days N] [--all] [--session FILE] [--top N]"
---

# cut-context: audit

Show the user what their tokens are actually spent on, from their own logs, and
the one or two fixes worth making. Measure; don't claim.

What the user typed after the command: `$ARGUMENTS`

## What this skill touches

It works in Claude Code only, because it reads Claude Code's own session logs,
which live outside the project folder. That is the whole point of the audit.

- **Reads:** `~/.claude/projects/` (session logs, including `subagents/`), and
  `CLAUDE.md` files to measure their size.
- **Writes:** one small JSON snapshot per run to `~/.claude/cut-context/audits/`
  (`--no-save` turns it off), plus backups in `~/.claude/cut-context/backups/`
  only when the user asks for a fix to be applied (Step 3).
- **Never:** makes network calls, runs other programs, installs anything, or
  deletes files. `audit.py` uses the Python standard library only and is plain,
  readable code.

## Step 1: run the audit

```bash
python3 "${CLAUDE_SKILL_DIR}/audit.py" $ARGUMENTS
```

If the path isn't expanded, use the base directory Claude Code printed for this
skill. The script reads `~/.claude/projects/` and writes one snapshot to
`~/.claude/cut-context/audits/` for the next comparison (`--no-save` skips it).
It makes no network calls. If it reports no logs for this project, suggest
`--all` or `--days 30`.

## Step 2: read it to the user

Keep it short. Copy the numbers from the report exactly; never round a small
number up or invent one.

1. **The verdict line**, word for word. If it says nothing is worth fixing, say
   so and stop there. That's a valid result.
2. **Where the cost goes:** one sentence. For example: "76% of your cost is
   Claude re-reading context; its replies are 6%."
3. **The top two or three findings**, each with its share and its fix in plain
   words. Say once that findings overlap, so their shares don't add up.
4. **If a previous audit exists**, the per-request change, and say it's per
   request so a lighter week doesn't look like a saving.

## Step 3: fixes, only if the user asks

Default to **manual mode**: give the exact change and let the user make it. If
the user asks you to apply a fix that edits a file (for example, moving sections
of CLAUDE.md into a skill):

1. Copy the original to `~/.claude/cut-context/backups/<YYYYmmdd-HHMMSS>/`, keeping
   its path, before touching it.
2. Show the diff and get an explicit yes.
3. After applying it, tell the user how to undo it (copy the backup back), and
   suggest re-running the audit after a few days of normal work.

Never change settings, hooks, MCP configuration or permissions on your own, even
when a finding points at them; describe the change instead. Habits (`/clear`
between tasks, running browser checks in a subagent) are advice, not edits.

## Limits, say them once if asked

- Tool-result sizes are estimates (about 4 characters per token, about 1,600
  tokens per image). Token totals come straight from the logs' usage fields.
- Dollar figures appear only when the logs contain Claude Code's own cost
  records, and they are approximate. Shares are exact relative to the weights
  (cache read 0.1x, cache write 1.25x or 2x, output 5x input).
- Pro and Max users pay in limit headroom, not dollars. The shares still show
  what uses up the limit.
