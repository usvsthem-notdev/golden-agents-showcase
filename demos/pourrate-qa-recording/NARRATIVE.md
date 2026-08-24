# PourRate QA recording

A live walkthrough recorded 2026-08-23 to QA-test the newest goldthread
console features (Model Router, review gate, thesis-conformance studio
checklist, cost rollup) against a fresh, independent project — chosen
specifically because it's a hard case: a social app about an alcoholic
beverage, which puts real weight on the thesis actually being enforced
rather than just present.

Raw material for a future demo. This session's browser tool doesn't export
screenshots to disk, so what's captured here is the narration plus the real
underlying evidence instead — exact prompts, exact model output, task ids,
timings, and file contents, all reconstructable against the live
`kanban.db` and the `pourrate` git history at
`~/Downloads/../scratchpad/pourrate` while it still exists. Turning this
into an actual visual demo (screen recording or a `goldthread_spinup.html`-
style walkthrough) is a follow-up task, not done yet — flagging that now
rather than implying screenshots exist when they don't.

## The project

**PourRate** — a social app for rating Guinness pours as a craft (head
retention, cascade, dome, presentation), not a drinking-tracker. Thesis
sealed with six rules chosen to be mechanically checkable, the same pattern
used for Atelier: a hard age gate before any content renders, ratings that
can never be about quantity/speed, no gamification keyed to volume, report
and block shipping with posting day one, identity minimization, and a
permanent (non-dismissible) responsible-drinking note wherever pour content
renders. iOS's guideline 1.4.3 (physical harm / alcohol) is the real-world
constraint this thesis is standing in for.

## What follows

Chronological log of the run: setting up the project, using the Model
Router to decompose the build goal, watching what a large model (Fable)
proposed once it had the thesis in context, dispatching and reviewing the
work, and the bugs (if any) found along the way.

## 1. Model Router: Fable plans against the thesis

Goal given to the router, assignee `gt-infra`, planner `claude-fable-5`,
executor `claude-sonnet-5`, cost-aware routing on, 3-5 subtasks requested:

> Build the PourRate social feed: a photo-based rating app for Guinness
> pours (head retention, cascade, dome, presentation). Must be iOS App
> Store compliant for alcohol-related content under guideline 1.4.3 — a
> hard age gate before any content renders, no promotion of
> quantity/frequency of drinking, no gamification tied to volume, report
> and block shipping day one, and a permanent responsible-drinking note
> wherever pour content renders. Handle-based identity only, no real name
> or phone number required.

**Result, 27 seconds, one real Fable call:** 5 subtasks, one per thesis
core rule — not a coincidence, the planner had the sealed thesis injected
into its context via goldthread's `pre_llm_call` hook (this is the whole
point of running the planner in a goldthread-covered spoke rather than a
bare API call). Cost-aware routing correctly separated the two
safety/compliance-critical subtasks from the routine ones:

| # | Subtask | Difficulty | Routed to |
|---|---|---|---|
| 1 | Build hard age gate with persistent age-assurance state | **high** | `claude-fable-5` |
| 2 | Implement pour rating model and composer scoped to pour craft only | medium | `claude-sonnet-5` |
| 3 | Build feed with permanent responsible-drinking notice on every content surface | medium | `claude-sonnet-5` |
| 4 | Ship report and block with server-side enforcement | **high** | `claude-fable-5` |
| 5 | Implement handle-only identity with minimal-data signup | medium | `claude-sonnet-5` |

The planner's own judgment lines up with what a careful human reviewer
would flag as the two subtasks most likely to have subtle correctness or
compliance failures if done sloppily — age gating and moderation
enforcement — and routed exactly those back to itself. All 5 filed as real
kanban tasks in Backlog under goldthread.

## 2. Bug found: router subtasks could never dispatch

Tried to actually run the age-gate subtask (`specify` → should reach
`ready` → `running` within ~60s, per how every other triage task in this
project has behaved). It didn't. It sat at `todo` indefinitely.

`hermes kanban promote t_0c614680` gave the real reason:

> cannot promote t_0c614680: unsatisfied parent dependencies: t_e8dc9116
> (use --force to override)

`t_e8dc9116` is the `[plan]` umbrella task the router had linked each
subtask to via `--parent`, believing that was a harmless grouping label.
It is not. Hermes treats `--parent` (and the separate `link` verb, same
underlying `task_links` table) as a real dependency edge: a child cannot
promote until its parent reaches `done`. The umbrella task is never meant
to be claimed or completed — it's a record of the goal — so every subtask
the router filed was **permanently stuck**, forever, by construction. This
would have silently broken the router for anyone who used it, since
nothing in the UI or the CLI error surfaces until you specifically try to
dispatch a child and go looking for why it won't move.

Root cause confirmed by testing the fix hypothesis directly:
`hermes kanban unlink t_e8dc9116 t_0c614680` immediately unblocked it —
the task was `ready` within the same call.

**Fixed in server.py's `run_route`:** subtasks are no longer `--parent`'d
to the umbrella task at all. The umbrella's id is referenced in each
child's body text (`"(Part of the plan filed as t_xxx.)"`) for human
traceability instead — informational, not a Hermes dependency. Verified
the fix at the code level with a fresh minimal route (not just the manual
`unlink` workaround on the original five): a new child task filed with
zero `task_links` rows, and went `specify → todo → ready → running` on its
own with no manual intervention. The same call also confirmed the third,
untested cost tier — a `low`-difficulty subtask correctly routed to
`claude-haiku-4-5-20251001`, the cheapest model, closing the loop on all
three routing tiers (low/medium/high → haiku/sonnet/fable) actually being
exercised in this session.

## 3. Watching the real work — and what it revealed about the thesis

Dispatched the age gate (Fable) and the rating composer (Sonnet) for real.
Both ran 20-30 minutes and produced substantial, genuinely good work:

- **The age gate is defense-in-depth, not a checkbox.** Three independent
  enforcement layers — routing (the content branch of the view hierarchy
  doesn't exist pre-pass), data (`FeedStore` is a no-op unless passed),
  and network (`GatedNetworkClient` re-checks and throws before any
  request is even constructed). Fails closed on corrupted storage. No
  "skip" or "remind me later" state exists in the state machine at all.
  This is exactly the kind of subtlety that justified the planner routing
  it to itself rather than the executor tier.
- **Rule 2 (rate the pour, not the drinking) is enforced at the API
  boundary, mechanically.** The Pydantic rating model uses
  `extra = "forbid"`, so any field outside the four closed craft
  dimensions — a hypothetical `quantity`, `pints_consumed`, `speed` —
  fails validation with an HTTP 422, not a comment asking nicely.
- **Interesting divergence:** the age-gate worker built a real Swift
  Package (`Package.swift`, SwiftUI views, XCTest) — because the goal
  explicitly said "iOS App Store compliant," and native code is a more
  honest answer to that than an HTML mockup would have been. The rating
  composer worker, with no such framing, built a React/TypeScript
  frontend plus a Python FastAPI backend. Nothing in the router's
  subtask prompts establishes a shared tech stack, so independent
  subtasks can legitimately diverge — not a bug, since forcing a single
  stack would have been wrong for the age-gate case, but worth knowing
  before routing subtasks that need to share code.

## 4. Bug found: the cost rollup silently showed unpriced runs as free

The age-gate run — 58 tool calls, 54,103 output tokens, 176,981
cache-write tokens on `claude-fable-5` — reported `cost_usd: 0.0`. Not
close to free; reported as literally zero.

The raw session export explained it: `cost_status: "unknown"`,
`cost_source: "none"`. A same-project Sonnet run in the same session
showed `cost_status: "estimated"`, `cost_source: "official_docs_snapshot"`.
Hermes simply has no price-table entry yet for `claude-fable-5` (a very
new model) or for the specific dated Haiku snapshot id the router picked
as its cheapest tier — and its estimator silently falls back to zero
instead of surfacing "unpriced." A cost rollup that can't tell "free" from
"unknown" isn't trustworthy, which is the one thing a cost rollup has to
be.

Investigating it surfaced a second, smaller bug in the same code path:
`project_cost`'s per-task total used `if task_total:` to decide whether a
task "had recorded cost" — and in Python, a real `$0.00` is falsy, so a
fully-cached, genuinely-free run silently fell out of the count even
though the dollar total was already correct. Confirmed directly: the
rollup read *"1/9 tasks with recorded cost"* before the fix and *"3/9"*
after, with the same `$2.86` total the entire time — proof the sum was
never wrong, only the bookkeeping around it.

**Fixed both**, in server.py: unpriced runs are now excluded from the
total (rather than counted as free) and reported separately — the artifact
panel says *"cost unpriced (no rate for claude-fable-5)"* per run, and the
project rollup says *"2 runs excluded (unpriced model — total is a floor,
not the full spend)"*. Verified no regression against Atelier and Cat
Gossip, whose all-Sonnet runs were always priced and whose totals didn't
move.

## Summary: what this run found and fixed in goldthread itself

Two real, independently-confirmed bugs, both found by actually trying to
use the features rather than reading the code — the exact value of a QA
pass over a design review:

1. **Model Router subtasks could never dispatch.** `--parent` (and the
   `link` verb — same underlying table) creates a real Hermes dependency,
   not a label; linking subtasks to a `[plan]` task that's never meant to
   be completed left every subtask permanently stuck. Fixed by dropping
   the link entirely and putting the parent's id in the child's body text
   instead — informational, no Hermes semantics.
2. **The cost rollup couldn't distinguish "free" from "unpriced."** Newer
   models Hermes hasn't priced yet (Fable, one Haiku snapshot) reported as
   a literal `$0.00`. Fixed to exclude and flag them instead of silently
   including them as free — plus a real truthiness-bug fix in the same
   code path.

Both are now fixed, verified against real dispatched work across two
independent projects, and pushed. One additional finding recorded but
deliberately not patched mid-run (a scope decision, not an oversight): the
Design & UI studio only discovers artifacts in a project's `design/`
folder, and nothing enforces that convention on router-generated subtasks
— PourRate's real UI work landed in `web/` and `src/`, invisible to the
studio. Worth a decision on whether to widen the studio's scan paths or
leave it as a documented convention.

## 5. Closing the loop: the review-gate's actual verdict

The thesis review filed against the age gate (`t_1ac77a40`) sat undispatched
through the whole QA pass — the review *gate* mechanism (badge, link,
thesis-aware acceptance criteria) had been verified, but never an actual
review verdict. Dispatched it for real.

**gt-review didn't rubber-stamp it.** It checked out the deliverable branch
into a disposable worktree and *independently re-ran the test suite*
(`26/26 assertion-groups passed, exit 0`) rather than trusting the parent
task's own handoff summary, then cited exact file+line for every one of the
six rule verdicts. All six: **PASS**.

The most valuable thing it produced wasn't a pass/fail — it was catching a
real, cross-task gap the QA pass itself never would have: **"confirm, at
the release level, that whatever ships posting already has report and
block shipped alongside it."** The age gate correctly doesn't violate rule
3 on its own (it introduces no posting surface), but the composer
(`t_e8c23e1b`, merged earlier) *does* let you post, and report/block
(`t_92411ff6`) was still sitting undispatched in Backlog the whole time.
The reviewer noticed the release-level inconsistency that two independently
"passing" subtasks combine to create — the exact class of problem
per-subtask review is supposed to be weak at, caught anyway.

Six non-blocking findings alongside the pass: a synchronous Keychain write
that could hitch the main thread, a redundant (harmless) double-dispatch,
the hardcoded region-agnostic age-21 threshold (already disclosed, correctly
not re-flagged as new), and an honest admission that accessibility/contrast
verification needs real Xcode tooling this machine doesn't have — disclosed
as an open gap rather than glossed over.

Cost: $1.04, real and priced (Sonnet). Project total after this run: **$3.90
across 4 priced runs**, with 2 runs still excluded as unpriced (Fable,
Haiku) — the cost-accuracy fix from earlier in this session holding up
correctly on fresh data.

This is the piece that makes the whole review-gate feature's case: thesis
rules as acceptance criteria didn't just produce a checklist that got
checked off — it produced a reviewer that verified independently, cited
precisely, and caught a real integration gap between two separately-shipped
pieces of work.
