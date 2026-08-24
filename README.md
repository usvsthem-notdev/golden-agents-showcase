# golden agents

Tooling around **goldthread** — a Hermes Agent plugin that holds a project to
an immutable "gold thesis": enforced at the tool boundary, re-anchored after
every context reset, with a kanban-backed PM window and contracted spoke
profiles.

The plugin itself lives in its own repo,
[usvsthem-notdev/goldthread-hermes-showcase](https://github.com/usvsthem-notdev/goldthread-hermes-showcase).
This repo is the work *around* it: how we picked models for each role, and
the console we run it from.

## What's here

| Path | What it is |
| --- | --- |
| [`console/`](console) | Live desktop console over real goldthread state — hub-and-spoke view, per-project projections, editable kanban |
| [`bakeoff/`](bakeoff) | Repeatable harness that scored local models per role, plus the full raw results and transcripts |
| [`demos/`](demos) | Standalone HTML walkthrough of a project spinning up under goldthread |

## Start here

Read [`HOW_TO_USE.md`](HOW_TO_USE.md) — prerequisites, the normal
day-to-day workflow, and what to do when something looks wrong.

```bash
python3 console/server.py     # → http://127.0.0.1:9120
```

Read [`console/README.md`](console/README.md) next — it documents the two
constraints that shape the whole design (the console never writes
`kanban.db`; the thesis has no write path at all) and the live-fire incident
that produced the dispatch-deliberately default.

## The results, briefly

A six-dimension bake-off across four local models produced a per-role
assignment rather than one winner:

- **gt-dumbq → `gpt-oss:20b` (local).** The one confident result: 1.7–3.4×
  faster than every alternative and fewest tokens.
- **gt-pm → `mistral-small3.2:24b` (local).** Least-bad, not good — the only
  model that ever recovered from a guard block, and the only one to pass a
  return contract at all.
- **gt-research / gt-infra / gt-bakeoff / gt-review → cloud, as a stopgap
  (2026-08-19 through 2026-08-24).** Local models scored 3/12 on contract
  compliance and **0/4** could drive the kanban tools unassisted, so these
  four roles ran on a cloud model while that gap stood.

The honest caveat is in [`bakeoff/README.md`](bakeoff/README.md): those six
dimensions measure *protocol adherence*, not domain skill. `bakeoff/cloud_evals.py`
covers the latter for the four cloud roles.

**Reverted to local on 2026-08-24** — the stopgap was never meant to be
permanent, and none of the four roles' local-model weaknesses had actually
been fixed since the bake-off; what changed is the failure mode, not the
model. All six profiles now default local. Cloud is no longer a parallel
default: a task only reaches it if its local run genuinely fails (a real
`blocked` status backed by a crashed/timed-out/gave-up run, not just any
block), via a one-click escalate control in the console that pins that one
task to Sonnet or Opus and redispatches it. Confirmed live the same day: a
real `gt-infra` task crash-looped 4× locally exactly like the bake-off
predicted, landed in that escalatable state, and completed once escalated.

One dimension is deliberately unmeasured. Hermes clamps compaction to 75–85%
of the context window for any model under 512K, which makes
thesis-adherence-under-compaction prohibitively expensive to test on ~64K
local models. That's recorded as a finding, not silently skipped.

## Pocket digest (fix 6, partial)

A real weekday-8am PM sweep cron job exists (`hermes -p gt-pm cron list`),
matching goldthread's own `cron-pm-sweep.md` spec: runs `gt-pm-sweep`
across all boards, `[SILENT]`-suppresses when there's nothing to report,
delivers to `origin`.

**Two things it still needs, both deliberately left for you:**

1. **A gateway scoped to `gt-pm`.** The running gateway (supervised by the
   desktop app; had PID 47024 the one time this was checked, 2026-08-19 —
   don't expect that number to still be current) only services the
   *default* profile — confirmed by comparing `hermes cron status` against
   `hermes -p gt-pm cron status`: the job exists and is valid, but nothing
   will ever fire it.
   `hermes -p gt-pm gateway install` starts a persistent background
   service — a standing system change, so it wasn't started without you
   saying so.
2. **A real delivery target.** `origin` only reaches the session that
   created the job. For it to reach your phone: connect a messaging
   platform (`platform_toolsets` already lists telegram/discord/signal/
   whatsapp/slack in `config.yaml`), get a bot token, `/sethome` in that
   chat, then `hermes -p gt-pm cron edit <job-id> --deliver <platform>`.

## Conventions

- **Raw evidence is committed.** `bakeoff/results/` holds every transcript,
  including the failures. The failure modes were always more useful than the
  aggregate scores.
- **Findings live next to the code that found them.** Where a comment
  explains a non-obvious constraint, it's usually because testing produced a
  surprise worth not re-learning.
