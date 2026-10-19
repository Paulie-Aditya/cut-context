# Benchmark results

Claude Code 2.1.303, `--model haiku`, 5 runs per arm. Each run uses a fresh copy of the fixture project (`bench/fixture.py`) with user settings, MCP servers and skills off. Tokens and dollars come from Claude Code's own `--output-format json` usage report.

| Scenario | Fix | ¢ per run without → with | Change | Weighted tokens without → with | Tasks passed without · with |
|---|---|---|---:|---|---|
| Six unrelated tasks: one long session vs /clear between tasks | /clear between tasks | 0.96¢ → 1.13¢ | **+17%** | 96k → 113k | 30/30 · 30/30 |
| One heavy task, then five small ones: keep going vs /clear after the heavy one | /clear after the heavy task | 2.00¢ → 1.80¢ | **-10%** | 200k → 180k | 30/30 · 30/30 |
| Three questions about a 2,700-line file, in one session | CLAUDE.md: grep, then read ranges | 0.26¢ → 0.26¢ | **+3%** | 26k → 26k | 15/15 · 15/15 |
| Fix a failing test in a 1,200-test suite, then two follow-ups | hook: keep the last 40 lines | 0.50¢ → 0.39¢ | **-21%** | 50k → 39k | 15/15 · 15/15 |

Cost is in input-token equivalents (ITE): cache read 0.1x, 1-hour cache write 2x, output 5x. Dollar ranges per run: long-session without $0.0075–$0.0155; long-session with $0.0090–$0.0161; after-big-task without $0.0105–$0.0231; after-big-task with $0.0167–$0.0195; big-file without $0.0024–$0.0027; big-file with $0.0024–$0.0031; noisy-output without $0.0041–$0.0061; noisy-output with $0.0035–$0.0044.


## Tokens processed per task (after-big-task)

| Task | Keep the session | /clear after task 1 |
|---|---:|---:|
| 1 | 123k | 135k |
| 2 | 254k | 72k |
| 3 | 64k | 36k |
| 4 | 190k | 56k |
| 5 | 65k | 36k |
| 6 | 65k | 37k |

## Tokens processed per task (long-session)

| Task | One long session | /clear between tasks |
|---|---:|---:|
| 1 | 59k | 77k |
| 2 | 50k | 31k |
| 3 | 114k | 127k |
| 4 | 84k | 43k |
| 5 | 54k | 53k |
| 6 | 55k | 28k |

**Follow-up tasks only (after-big-task, tasks 2-6):** 0.87¢ → 0.49¢ per run (-43%).
