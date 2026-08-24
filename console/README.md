# goldthread console

A live desktop view over real goldthread state. Not a mockup — every number
comes off disk.

```bash
python3 console/server.py                              # → http://127.0.0.1:9120
python3 console/server.py --project "Name=/path/dir"   # extra ad-hoc thesis dir
```

Zero dependencies (stdlib only), so it runs on the system python with
nothing to install.

## Projections (the sidebar)

The left rail lists views over the same board, one per project:

- **All work** — every task, thesis via the default resolution.
- **One per Hermes project** — created with `hermes project create NAME PATH`
  and discovered automatically from `projects.db`. Tasks filed with
  `--project <slug>` scope to it; the thesis is read from the project's
  primary folder.
- **Unfiled** — appears only when tasks exist with no `project_id`, so a
  per-project filter can never silently hide work.
- **Ad-hoc dirs** (`--project NAME=PATH`) — shows that directory's thesis;
  board scope stays empty and the view says why (make it a real Hermes
  project to scope tasks to it).

The selected view persists in the URL hash (`#v=slug`), so a reload keeps
your place.

## What it reads

| Source | Used for |
| --- | --- |
| `~/.hermes/kanban.db` | the board — tasks, statuses, assignees (**read-only**) |
| `~/.goldthread/pm-ledger.jsonl` | claimed / completed / blocked lifecycle events |
| `GOLD_THESIS.md` | core problem, definition of done, core rules, file mode |
| `~/.hermes/profiles/*/config.yaml` | which model actually runs each role |

Thesis resolution follows the same order `inject.py` uses
(`GOLDTHREAD_THESIS_PATH` → project dir → `~/.hermes`), so the console and
the running agents can never disagree about which file is the thesis.

## Editable board — through the CLI, never the database

The board is editable from the PM window and spoke windows: file tasks,
promote, request review / changes, block (with a reason), unblock, complete,
reassign. Every edit shells out to `hermes kanban ...` — the console never
writes `kanban.db` itself, because the dispatcher writes that database on a
60s gateway tick and a second writer racing it would corrupt state that is
supposed to survive restarts. The CLI's own errors surface verbatim in the
window, so a refused transition tells you exactly why.

Two deliberate absences:

- **No "start" button.** `ready → running` happens when a worker claims a
  task through the dispatcher; `claim` demands worker ownership semantics
  (worktrees, run ids). The console is the PM surface — it dispatches,
  reviews, unblocks, and closes; it does not impersonate workers.
- **No thesis editing.** The board is editable; the thesis is not.
  Amendments stay chmod 644 → edit → chmod 444 → `thesis:` commit, by a
  human.

## Human-only tasks

A task can be marked human-only — filed straight to `blocked` (never
`triage`, so `auto_decompose` can't touch it even if re-enabled), tagged
`tenant=human-only`, and rendered with zero action buttons: no unblock,
specify, promote, or reassign. `/api/kanban` refuses every write for such a
task server-side — looked up fresh from `kanban.db` on every request, not
trusted from the client — so this isn't just hidden UI.

**The honest limit**: this is enforcement at the console's own write
boundary, not a Hermes-level guarantee. The raw `hermes kanban unblock` CLI
and the desktop dashboard both bypass it completely — Hermes has no
first-class "never dispatch this, even across unblock" concept.
`block_kind=needs_input` is not special-cased against unblock in the
dispatcher (confirmed by reading `block_task`'s own code: unblocking a
needs_input task returns it to the exact same claimable pool as a transient
block). This was found the hard way: a task explicitly blocked as
human-only was unblocked and completed by a real worker, with the
completing run's own output empty (`result_len: 0` — no real work was
even done) yet the task showed `done`. A human-only marker is the console
refusing to be part of the problem; it cannot make Hermes itself refuse.

`tenant` was chosen over a title prefix because `specify` rewrites titles
(confirmed live — grooming a triage task changes its title) and neither
`block` nor `unblock` touch `tenant`. A title prefix (`[HUMAN-ONLY] ...`) is
still checked as a fallback for tasks tagged before this existed.

**Creating a task is a chat prompt, not a title field.** The new-task box is
a multi-line textarea: the first line becomes the task's title, everything
after the first newline becomes `--body` — the full spec a worker actually
reads. A one-line prompt behaves exactly like the old title-only form.
⌘/Ctrl+Enter files it; plain Enter inserts a newline, since it's a prompt now,
not a single-line input.

**Model picker, scoped to what's actually wired up.** Next to the assignee
dropdown is a model picker for pinning one task to a specific local model —
"try this on `qwen3.5:4b` before committing the profile to it" — without
touching the assignee's default config (`hermes kanban create --model
--provider`). Enabled for all six profiles as of 2026-08-24: the four
profiles that ran cloud-only during the 2026-08-19 stopgap (`gt-research`,
`gt-infra`, `gt-bakeoff`, `gt-review`) were reverted to local and gained a
`providers.ollama-local` block, matching `gt-pm`/`gt-dumbq`. Each option
carries a hint where one's been stated (`qwen` → "better for coding",
`mistral` → "better for text"). The picker reads this list from
`/api/models` rather than a client-side copy — a hardcoded copy is exactly
what let it drift once already (still showed `gt-pm`/`gt-dumbq` only, for a
few commits after the server started returning all six). A picker that
looks live for a profile it doesn't actually work for is worse than one
that's honestly scoped — the server still rejects a model override paired
with an unconfigured assignee even if a client somehow sent one anyway.

**Dispatch is deliberate — but only if `auto_decompose` is off.**

New tasks file to Backlog (`triage`) by default; the "dispatch now" checkbox
files them `ready` instead. That default exists because of a real incident:
the Hermes desktop app runs a gateway, and three freshly filed `ready` tasks
were claimed by real cloud workers within a minute. (That incident also fixed
gateway detection — the desktop gateway runs as `hermes_cli.main gateway`,
which the original pgrep pattern missed.)

**Filing to Backlog does not, by itself, hold work.** Hermes'
`kanban.auto_decompose` defaults to **true**: the dispatcher grooms triage
tasks — rewriting their titles — promotes them to `ready`, and claims them.
Dogfooding caught this: a task filed to Backlog was rewritten and dispatched
40 seconds later. An earlier version of this document claimed the Backlog
default parked work; that was false assurance, which is worse than no
assurance at all.

So the console reads the live config and says which regime you are in:

| `auto_decompose` | What Backlog means | Console shows |
| --- | --- | --- |
| `true` (stock) | nothing parks; filing starts an agent | amber warning banner |
| `false` | Backlog genuinely holds | quiet, or a ready-count note |

To make Backlog mean what it looks like:

```bash
hermes config set kanban.auto_decompose false
```

The write endpoint is guarded against localhost CSRF: it requires a custom
header (forcing a CORS preflight that is never answered for cross-origin
callers) plus an Origin check, JSON only, strict per-action input
validation, and a fixed verb allowlist — nothing not built in `kanban_argv`
can run.

It also binds to `127.0.0.1`, not `0.0.0.0`. The page exposes board contents
and the full thesis; reaching it from another device is a network-exposure
decision worth making deliberately rather than inheriting as a default.

## What it surfaces that is easy to miss

- **No thesis / writable thesis.** If `GOLD_THESIS.md` is missing, nothing is
  anchoring the project and no thesis is being injected. If it exists but
  isn't `chmod 444`, the Python guard still blocks agent writes but the
  OS-level layer is off. Both raise a banner.
- **Gateway not running.** The dispatcher lives inside the gateway, so
  without it tasks sit in `ready` forever and the board looks deceptively
  calm. This is the most common cause of a silent board.
- **Unset profile models.** A spoke with no `model.default` will fall back to
  whatever the global default is, which is rarely what you intended.

## Columns

Hermes' real status vocabulary is `triage, todo, scheduled, ready, running,
review, blocked, done, archived`. The board groups them into six columns but
every card still shows its raw status, so the grouping never hides what the
system actually thinks. `archived` is excluded.

## Windows

Click any card to open it full-screen; the close button returns you to the
whole view. The hub and the board strip both open the PM window (board +
thesis + ledger); each spoke opens its own model config and assigned work.

## Design & UI studio

The bar below the board strip opens a design surface for the current
project — a Figma-ish view of the UI work, built entirely from real files
on disk, nothing mocked:

- **Frames.** Every `*.html` in `design/`, `web/`, or `public/` (checked in
  that order), rendered live in a device-switchable frame (Desktop / Tablet
  / Mobile). The preview is the *real* artifact — its own CSS and its own
  JavaScript running — so the form actually validates, the counter actually
  counts. Each frame is an `<iframe sandbox="allow-scripts">` fed via
  `srcdoc`: scripts run, but **without** `allow-same-origin` the artifact
  gets an opaque origin and cannot touch the console's DOM, cookies, or
  storage. Verified live — a script inside the frame reaching for
  `parent.document` gets a `SecurityError`.

  `design/` was the only place ever checked until it turned out that was a
  convention, not a rule — it held only because Cat Gossip's and Atelier's
  task *prompts* explicitly said "build design/X.html." Nothing enforces
  that on auto-generated work: routing a goal through the Model Router
  produced a real UI in `web/composer.html`, invisible to the studio until
  this widened. Real UI source the studio genuinely can't render — React
  `.tsx`/`.jsx`, Vue, Swift, Kotlin, found up to two folders inside `src/`,
  `Sources/`, or `app/` — is listed underneath the frames instead of going
  unmentioned; no browser executes that without a build step goldthread
  doesn't run, so it's a pointer list, not a preview.
- **Tokens.** Color swatches, a type specimen set in the project's own font
  stack, the spacing scale as proportional bars, and the corner-radius
  samples — all parsed from the design artifact's actual `:root` CSS block
  (the implemented source of truth, which `design-system.md` itself tells
  you to copy), not from prose. Each color's "use" description is joined in
  from the `docs/design/design-system.md` table when it's present. A
  `var(--x)` font reference is resolved back to its real stack, so the
  specimens render in the actual typeface rather than an unresolved token.
- **Thesis check.** The project's gold-thesis Core Rules as an eyes-on
  checklist you fill in *while looking at the live frame above* —
  pass / fail / n-a per rule, plus a note field for the file+line. "Copy
  as review comment" lifts it into paste-ready text. This is the human
  counterpart to the board's `thesis review →` button (which dispatches an
  AI review); the studio is where a person does the review, with the
  artifact running in front of them. State survives the poll (this window
  is out of the re-render loop) but not a reload — it's a working pad, not
  storage.

A project with no `design/` folder gets an honest empty state pointing at
where UI work lands, not a blank panel. This window is fetched once on open
and deliberately left out of the 5-second poll loop — design artifacts
don't change under you mid-session, and polling would wipe the live frames
and your device selection.

## Review gate & thesis conformance

goldthread guards the thesis file and re-injects it into worker context,
but nothing checked that what a worker *produced* actually conformed — the
enforcement was write-only. The board closes that loop:

- Every **done** ticket shows a **`✓ reviewed`** or **`⚠ unreviewed`**
  badge. "Reviewed" means a `gt-review` task is linked to it (`task_links`),
  so it's a real, checkable state — not a vibe.
- An unreviewed deliverable gets a **`thesis review →`** button. It files a
  `gt-review` task whose acceptance criteria *are* the project's thesis Core
  Rules — one PASS/FAIL line each, "cite the file+line" — and links it to
  the deliverable. The review is thesis-aware by construction. It parks in
  Backlog; you dispatch it when ready.

**Per-task review has a real blind spot: some rules are only satisfiable by
several deliverables existing together.** Found live — the age gate's own
review correctly passed "report and block ship alongside posting" for
*that diff* (it introduces no posting surface), then had to hand-flag in
prose that the rule actually needs the already-merged composer *and*
report/block together, and nothing checked that combination on its own.
The PM window's **`check release conformance`** button closes this: it
files a `gt-review` task scoped to the project's *current default branch* —
everything merged so far, i.e. what would actually ship — and asks for a
verdict per rule against the whole codebase, not one diff. First real run
on PourRate came back **FAIL**: it independently found that the iOS age
gate is excellent but covers only the Swift surface, while `web/` +
`backend/` — the *only* surface that can actually post or list content —
has no age gate or report/block at all. Two structurally separate
codebases, one thesis; a rule enforced in one does not propagate to the
other. See "Router subtask divergence" below for why that split happened
in the first place.

## Cost rollup

The PM window's **`∑ project cost`** button sums real run cost (from
`hermes sessions export`) across every task in the current view. It's a
button, not a polled number, because each run costs a `sessions export`
shell-out — never make the 5-second tick do that N times.

## Model router

The **Model router** bar (below the design bar) delegates a goal across two
model tiers: a strong model *plans*, cheaper models *execute*.

- You write a goal and pick a **planner** (default `claude-fable-5`) and an
  **executor** (default `claude-sonnet-5`). The console runs one real
  planner call — `hermes -p <spoke> -m <planner> --provider anthropic chat
  -Q` — which decomposes the goal into subtasks. It runs in a spoke, so
  goldthread's `pre_llm_call` injects the gold thesis *into the planner's
  context*: it plans against the law, and it shows — plans routinely include
  a subtask to verify thesis rules (provenance, labeling) survive the change.
- Each subtask is filed as a real kanban task, pinned to a model via
  `--model … --provider anthropic`, referencing the `[plan]` task's id in its
  body — deliberately **not** a Hermes dependency link. Found live: Hermes'
  `--parent`/`link` both write the same `task_links` edge that `promote()`
  enforces as "child can't advance until parent completes," and the umbrella
  task is never meant to be worked or completed — linking it that way got
  every subtask permanently stuck at `todo` with "unsatisfied parent
  dependencies." `hermes kanban unlink <parent> <child>` clears an
  already-stuck task if you hit this from an older run.
- **Cost-aware per-subtask routing** (on by default, borrowed from the
  LLMRouter project's difficulty/cost idea): the planner rates each subtask
  `low` / `medium` / `high`, and the router maps that to a model —
  `low → cheapest` (Haiku), `medium → executor` (Sonnet), `high → planner`
  (Fable). So one decomposition can span three models, with the subtle,
  thesis-sensitive work going to the strongest one. Turn it off and every
  child gets the executor, flat.

Two guardrails worth knowing. The planner and executors run on the
**anthropic** provider, so the assignee needs a working `--provider
anthropic` override — checked live (`hermes -p <profile> auth status
anthropic`), not assumed from the profile's own default model. That check
would have gone quietly empty for every spoke the moment all six went
local on 2026-08-24, since it used to read the default-provider field
directly; fixed to check real override capability instead, and confirmed
live that a local-primary profile (`gt-pm`) still answers a real anthropic
call correctly — a profile's default model and its stored credential turn
out to be independent. The assignee list stays scoped to
`gt-research/infra/bakeoff/review` anyway: `gt-pm` is excluded as the hub,
not a work-executing spoke, and `gt-dumbq` because it's deliberately the
weak/dumb-question spoke — both product calls now, not a capability limit.
Everything the router files lands in **Backlog**; the planner call is the
only model spend until you dispatch the subtasks yourself.

### Router subtask divergence

Each subtask executes in an *isolated* worktree — separate workers can't see
each other's code as it's being written, only whatever's already merged
when the planner runs. "Independent subtasks" is a project-management
notion borrowed from how humans write tickets, and human tickets are
independent against a backdrop the router doesn't have: an existing
codebase, an established architecture, a team that talks to itself. Strip
that out and independent stops meaning parallelizable — it means each
worker starts from a blank slate and re-derives foundational choices with
no way to converge.

This is exactly what happened on PourRate the first time: an "age gate"
subtask and a "rating composer" subtask, decomposed in the same route call
with no shared context, came back as a Swift package and a completely
disconnected React+Python app. Not two parts of one product — two products
sharing a git repo. The [release conformance check](#review-gate--thesis-conformance)
found the real cost of that: the age gate is excellent, but it protects
only the Swift surface, and the *only* surface that can actually post
content has no gate at all, because nothing connects the two codebases. A
safety constraint implemented in one surface does not propagate to a
structurally separate one — confirmed by grepping the web/backend surface
for any reference to the age gate, Swift, or Keychain: zero matches.

**What this doesn't fix:** subtasks filed in the *same* route call still
can't see each other's in-flight work — that's a harder scheduling problem
(sequencing foundational subtasks before fan-out, or an integration pass
after) that isn't built.

**What is fixed:** the cheaper, avoidable half — a *second* route call
against a project that already has real code should not blindly re-derive
a different stack from scratch. Before the planner call, the console scans
the project the same way the [design studio](#design--ui-studio) does
(`design/`, `web/`, `public/` for HTML; `src/`, `Sources/`, `app/` for
non-renderable source) and tells the planner what already exists, with an
explicit instruction to extend it in the same stack unless the goal
genuinely calls for a separate surface — and if it does, to say explicitly
that the new subtask needs to be *wired into* any existing safety-critical
surface, not built beside it.

Verified live: routing an intentionally stack-ambiguous goal ("add push
notifications for comments") against PourRate a second time no longer
invented a third stack. It extended both existing surfaces by name — "Add
comment-notification dispatch to the FastAPI backend," "Wire push
notifications into the iOS app" — and the iOS subtask's body explicitly
named the existing gate files (`AgeGatePolicy`/`AgeAssuranceStore`/
`GateCoordinator`/`GatedNetworkClient`), required deep-link taps to route
through the gate before any content renders, and cited the specific thesis
rule numbers as acceptance criteria. That level of integration-awareness
didn't exist before the planner knew the gate was there to integrate with.
