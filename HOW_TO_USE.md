# How to use this repo

This is the entry point. Each subfolder has its own README with more detail
— this document is the map, and the order you'd actually do things in.

## What's here, in one line each

| Path | What it is |
| --- | --- |
| [`console/`](console/README.md) | Live desktop view over a real goldthread project — the thing you actually run day to day |
| [`bakeoff/`](bakeoff/README.md) | The harness that picked which model runs each spoke role |
| [`security/`](security/README.md) | Red-team harnesses that attack the guard's "agents can't touch the thesis" promise |
| [`demos/`](demos/goldthread_spinup.html) | A standalone, shareable walkthrough — no server needed, just open the file |

## Prerequisites

This repo is tooling *around* goldthread, not goldthread itself. Before any
of the below will do anything real, you need, already set up on this
machine:

- **Hermes Agent installed and on `PATH`.** Check with `hermes version` —
  this repo was built against v0.20.4.
- **The goldthread plugin installed** at `~/.hermes/plugins/goldthread`.
  Source: [usvsthem-notdev/goldthread-hermes-showcase](https://github.com/usvsthem-notdev/goldthread-hermes-showcase)
  (clone it wherever you like — the paths below assume the private working
  repo's own layout, `~/Downloads/goldthread-hermes`; adjust to wherever
  you actually put it). Its `install.sh` copies the plugin files and
  skills into place. Verify with `hermes doctor` or
  `hermes chat -q "run gt_selfcheck"`.
- **Six Hermes profiles** (`hermes -p <name> ...`), each with a model
  already configured in `~/.hermes/profiles/<name>/config.yaml`:

  | profile | role | model (as of 2026-08-24) |
  | --- | --- | --- |
  | `gt-pm` | hub / PM | `mistral-small3.2:24b` (local, ollama) |
  | `gt-dumbq` | quick/cheap questions | `gpt-oss:20b` (local, ollama) |
  | `gt-research` | research | `mistral-small3.2:24b` (local, ollama) |
  | `gt-infra` | infra/devops | `mistral-small3.2:24b` (local, ollama) |
  | `gt-bakeoff` | bake-off variant work | `mistral-small3.2:24b` (local, ollama) |
  | `gt-review` | code review | `mistral-small3.2:24b` (local, ollama) |

  Ran `gt-research`/`gt-infra`/`gt-bakeoff`/`gt-review` on cloud
  (`claude-sonnet-5`) from 2026-08-19 to 2026-08-24 as an explicit,
  temporary stopgap (see the main [`README.md`](README.md#the-results-briefly)
  for why) — reverted to local for all six. Cloud isn't gone: it's now a
  one-click escalation for a task whose local run genuinely fails (see
  step 6 below), not a parallel default. All six profiles now carry a
  `providers.ollama-local` block, so the console's model picker (below)
  works for every assignee, not just two. This whole setup was done by hand
  across several sessions, not by a script; there's no one-command way to
  reproduce it from scratch yet.
- **Ollama running locally** (`http://127.0.0.1:11434`) with the models
  above pulled, if you're using the local profiles.
- **A Hermes gateway running.** Without one, `ready` tasks sit forever —
  nothing dispatches. Check with `hermes gateway status` (or, per-profile,
  `hermes -p gt-pm gateway status`). The console's banner tells you this on
  every startup and every page load; don't skip past it.
- **Python 3**, stdlib only — nothing in this repo needs `pip install`.

If any of these aren't true yet, fix that first. The console will still
load and tell you what's missing (unset models render as a red "unset"
pill, a dead gateway gets its own banner), but nothing will actually run.

## The normal workflow

### 1. Start the console

```bash
python3 console/server.py --port 9120
```

Binds to `127.0.0.1` only, on purpose — this exposes your board contents
and thesis text, and that's not something to put on a shared interface by
default. Open `http://127.0.0.1:9120`.

Read the startup banner in your terminal before doing anything — it tells
you whether the gateway is running and whether `auto_decompose` is on. If
`auto_decompose` is on, **filing a task does not hold it**: it gets groomed
and dispatched within about a minute regardless of what you click. If you
want filing to actually park work, turn it off first:

```bash
hermes config set kanban.auto_decompose false
```

### 2. Pick a project (the sidebar)

The left rail lists every real Hermes project (`hermes project list`) plus
"All work" and, when relevant, "Unfiled" (tasks with no project — this
entry only appears when such tasks exist, so a filter can never silently
hide work). Click one to scope the whole page to it: its thesis, its board,
its task counts.

To add a new project as a sidebar entry without making it a first-class
Hermes project:

```bash
python3 console/server.py --project "Some Name=/path/to/dir"
```

### 3. Open the PM window

Click the gold "PM" tile in the center of the grid. This is the board:
columns for Backlog → Ready → Running → Review → Blocked → Done, the gold
thesis (read-only, always), and the lifecycle ledger.

### 4. File a task

The new-task box is a **chat prompt, not a title field**. Write what needs
doing — the first line becomes the task's title, everything after the
first newline becomes its full spec (what a worker actually reads). A
one-line prompt behaves exactly like a plain title. `⌘`/`Ctrl`+`Enter`
files it.

Next to it:

- **Assignee** — which profile/role does this.
- **Model** — enabled for any assignee now that all six profiles run
  local. Pins *this one task* to a specific local model without touching
  the profile's default — useful for trying a task on a cheaper or
  different model before committing to reassigning the role. Each option
  carries a hint where one's been stated (`qwen` → "better for coding",
  `mistral` → "better for text") — not a benchmark claim, just guidance
  surfaced next to the picker instead of left as tribal knowledge.
- **dispatch now** — unchecked by default, on purpose. A task is filed to
  Backlog (`triage`) unless you check this, which files it straight to
  `ready`, where the gateway can claim it within about a minute. Filing a
  task is never, by itself, the same as starting an agent — you have to
  say so.
- **🔒 human-only** — forces the task to `blocked` immediately, tagged so
  the console refuses to touch it again from here (see
  [`console/README.md`](console/README.md) for exactly what that does and
  does not guarantee — it's a console-boundary lock, not a Hermes-level
  one).

### 5. Watch it run

A `running` ticket has a **view run** button once it's running or done:
branch, commits, diff stat, and cost/tokens for the actual run(s), pulled
live via `hermes sessions export`. Cost is deliberately not cached while a
task is still running, so the number doesn't freeze mid-flight.

If a local run genuinely fails — status lands on `blocked` with a real
crashed/timed-out/gave-up run behind it, not just any block — the ticket
grows an **escalate → sonnet** / **escalate → opus** control. One click
pins that task's next dispatch to the chosen cloud model and returns it to
Ready; nothing else about the task changes. This is the only path back to
cloud now that all six profiles default local, and it's confirmed to work:
a real `gt-infra` task crash-looped 4× locally, landed here, and completed
once escalated.

### 6. Check dispatch health before you rely on a profile

Open any spoke tile → **check dispatch health**. For a local profile this
pings ollama and checks the model is actually pulled. For a cloud profile
it checks you're logged in *and* checks for the one env-var footgun that's
bitten this setup before: a stale `ANTHROPIC_API_KEY` in whatever shell
launched the console, which Hermes will prefer over a perfectly good stored
login. If you ever see `AuthenticationError` on a cloud dispatch, this is
almost certainly why — restart the console with that variable excluded:

```bash
env -u ANTHROPIC_API_KEY python3 console/server.py --port 9120
```

## The gold thesis — what you can and can't do here

`GOLD_THESIS.md` in a project's primary folder is meant to be unchangeable
by any agent. The console reflects this: there is no edit control for it
anywhere in the UI, on purpose. To amend one:

```bash
chmod 644 GOLD_THESIS.md
# edit it
chmod 444 GOLD_THESIS.md
git commit -m "thesis: <what changed and why>"
```

That chmod matters less than it looks like it should — `rm`, `mv`, and
`sed -i` all act on the directory entry, not the file's own permission
bits, so mode 444 alone does not stop them. The actual enforcement is
`guard.py`'s `pre_tool_call` hook, which blocks the write *attempt* before
it happens. If you're ever unsure whether that's actually holding, see the
next section — it's not something to just trust.

## Verifying the guard actually works

Don't take "agents can't touch the thesis" on faith. Run the attacks
yourself:

```bash
python3 security/guard_redteam.py     # which mutation attempts does it allow?
python3 security/guard_e2e.py         # do the allowed ones actually change a real file?
python3 security/guard_falsepos.py    # does ordinary work still go through?
```

These run against the *installed* copy (`~/.hermes/plugins/goldthread`),
not a copy sitting in this repo, and `guard_e2e.py` builds a real sealed
(`chmod 444`) thesis in a throwaway directory to test against — nothing
here touches a real project. Full writeup, including the six real breaches
these harnesses found on 2026-08-20 and how they were fixed, is in
[`security/README.md`](security/README.md).

Also run the plugin's own suite after touching `guard.py`:

```bash
cd ~/Downloads/goldthread-hermes && python3 tests/test_goldthread.py     # adjust the path to wherever you cloned it
```

74 tests as of the last guard fix (the public
[goldthread-hermes-showcase](https://github.com/usvsthem-notdev/goldthread-hermes-showcase)
copy runs the same 74, 13 of them as redacted stubs — see its README for
why). If you edit `guard.py`, edit **both** copies — the source tree and
the installed one — and diff them before you trust either:

```bash
diff ~/Downloads/goldthread-hermes/guard.py ~/.hermes/plugins/goldthread/guard.py
```

They have drifted apart before, so diff them after any guard edit. The
plugin source is now its own git repo
([usvsthem-notdev/goldthread-hermes-showcase](https://github.com/usvsthem-notdev/goldthread-hermes-showcase)),
so guard changes there have history; the red-team harnesses that exercise
the guard live in *this* repo's `security/` folder.

## Re-running the model bake-off

If you add a model, upgrade Hermes, or just want to re-check an assignment:

```bash
cd bakeoff
python3 run.py --preflight                        # sanity checks, no model turns
nohup python3 run.py > results/run.log 2>&1 &      # full matrix — takes hours
tail -f results/run.log
python3 run.py --summarize                         # rollup + disqualifier list
```

Every run appends to `results/matrix.jsonl`; nothing is overwritten, so old
transcripts stay available. Full scenario-to-dimension mapping and how to
run a subset are in [`bakeoff/README.md`](bakeoff/README.md).

## The shareable demo

`demos/goldthread_spinup.html` is one self-contained file — no server, no
build step. Open it directly in a browser, or serve it:

```bash
python3 -m http.server 8099 --directory demos
```

It simulates a project spinning up under goldthread for someone who wants
to see the idea without touching any real infrastructure.

## When something looks wrong

- **A ticket shows "Nothing in this view yet" right after you filed
  something.** Wait a poll cycle (5s) — this was a real out-of-order
  request race, fixed in console v12, but if you're on an older checkout
  you may still hit it.
- **Cost shows `$0.00` on a run you know wasn't free.** Fixed in v11 for
  sub-cent runs (renders 4 decimals below a cent); if you see it on a
  larger run, that's a real bug, not this one.
- **A cloud dispatch fails with `AuthenticationError`.** See the
  `ANTHROPIC_API_KEY` note above.
- **A task you filed dispatched immediately when you didn't check
  "dispatch now."** Check `auto_decompose` — if it's on, the gateway
  grooms and dispatches Backlog tasks on its own regardless of the
  checkbox. This is stock Hermes behavior, not a console bug, and the
  console warns about it on every load when it's true.
- **Anything involving the thesis.** Don't guess — run the harnesses in
  `security/` and read what they actually find.
