# Red-teaming the goldthread guard

> **Public-copy note:** the harnesses below have had their exact evasion
> payloads redacted — the techniques are generically useful against any
> similarly naive file guard, not just this one. The narrative and lessons
> below are otherwise unedited.

goldthread's whole promise is one sentence: **agents cannot change the gold
thesis.** That promise is worth exactly as much as the guard that enforces
it, so it gets attacked rather than admired.

These three harnesses are the attack. Run them against the *installed*
plugin (`~/.hermes/plugins/goldthread`), not a copy — drift between the
source tree and the installed copy has happened before.

```bash
python3 security/guard_redteam.py    # which mutation attempts does it allow?
python3 security/guard_e2e.py        # do the allowed ones actually change the file?
python3 security/guard_falsepos.py   # does ordinary work still go through?
```

`guard_e2e.py` is the one that matters. A guard-allowed command is only a
real breach if the thesis *actually changed*, so that harness builds a real
sealed (`chmod 444`) thesis in a throwaway directory, runs each command, and
compares hashes. Nothing here touches a real project.

## What this found (2026-08-20)

v0.3.1's shell path opened with `if THESIS_BASENAME not in cmd: return None`
— a literal, case-sensitive substring test against the command string. Any
way of naming the file that doesn't spell out `GOLD_THESIS.md` character for
character skipped the guard entirely. Four categories got through (exact
payloads redacted — see the note at the top of this file):

| Evasion category | What it exploits |
| --- | --- |
| glob expansion | shell expands a pattern to the real filename after the guard's string check |
| case variant | filesystem is case-insensitive, the guard's string match wasn't |
| quote splitting | the literal basename never appears unbroken in the command string |
| variable indirection | the filename is assembled at shell-eval time, not written literally |

**Six evasions reached the real file.** Two facts made it that bad:

1. **macOS ships a case-insensitive filesystem by default.** `gold_thesis.md`
   and `GOLD_THESIS.md` are the same file, but were not the same string.
2. **`chmod 444` is not a second layer.** `rm`, `mv`, `sed -i` and
   `chmod` operate on the *directory entry*, which needs write permission on
   the directory, not on the file. Verified: `rm -f` deleted a 444 thesis,
   `sed -i` rewrote one, and a glob-targeted `chmod 644` unsealed one
   outright. The redirect-style writes (`echo x > …`) were the *only* ones
   the mode bits stopped, which is what made the hole look smaller than it
   was.

The fix replaces string matching with path resolution: case-folded basename
comparison, real glob expansion against the working directory, `key=value`
splitting (`dd of=…`), and a deliberately narrow rule for shell expansion —
`$VAR` only counts when it sits in a path position (a redirect target, or an
argument to a mutating verb) *and* a thesis file is actually in reach.

That last narrowing is the whole design tension. An earlier attempt treated
any `$` as suspicious and blocked `echo $PATH > /tmp/path.txt`, which is
obviously not an attack. **A guard that blocks ordinary work is a guard that
gets switched off**, and a guard that is switched off protects nothing — so
`guard_falsepos.py` is not a nicety, it is half the specification.

## The guard was only half-installed (found 2026-08-22)

Hardening `guard.py` is worthless if the guard never runs. A diagnostic of a
real multi-spoke run found exactly that: **goldthread was enabled only in the
`default` profile.** Every spoke (`gt-research`, `gt-infra`, `gt-bakeoff`,
`gt-review`, `gt-pm`, `gt-dumbq`) had the plugin *absent*, because named
profiles discover plugins from their own `~/.hermes/profiles/<name>/plugins/`
dir — not the global `~/.hermes/plugins/` — and `install.sh` only ran
`hermes plugins enable goldthread` once (hitting default).

Consequences, all silent:

- **The guard never ran in the processes that do the work.** Workers run in
  their spoke profile. A `gt-dumbq` worker asked to `rm -f GOLD_THESIS.md`
  would have deleted the thesis — `rm` bypasses `chmod 444`, and there was
  no `pre_tool_call` hook there to stop it. All the red-teaming above tested
  `guard.py` in isolation; none of it checked whether the hook was *mounted*
  where workers actually run.
- **Thesis re-injection (`pre_llm_call`) never fired for spokes** either, so
  workers only "followed the thesis" when it was in their task body or
  readable in the worktree — never because goldthread anchored it.
- **The ledger recorded 27 claims but 3 completions across 28 finished
  tasks.** `kanban_task_completed`/`_blocked` fire in the *worker* process;
  with no plugin there, completions vanished. The PM sweep reads that ledger
  and would report everything as perpetually in-flight.

Fix: symlink the one installed copy into each profile's `plugins/` dir
(single source of truth, no drift) and `hermes -p <p> plugins enable
goldthread`. `install.sh` now does this in the profile-creation loop (in the
plugin's own repo,
[usvsthem-notdev/goldthread-hermes-showcase](https://github.com/usvsthem-notdev/goldthread-hermes-showcase),
under version control as of 2026-08-22). Proven after the fix: the `rm -f`
was blocked and the thesis survived; a completion fired into the ledger. The
console now runs a per-spoke coverage check and raises a hard alert if any
spoke loses goldthread, so this can't rot silently again.

The lesson worth keeping: **"the guard logic is correct" and "the guard is
running where it matters" are two different claims, and only the second one
protects anything.** Test mounting, not just logic.

## Standing gaps

- **The guard cannot see through dynamic expansion**, only refuse it. A
  command whose target is genuinely unknowable until the shell runs it is
  blocked when a thesis is nearby, not analyzed.
- **Enforcement stops at the tool boundary.** Anything that writes the file
  without going through a hooked tool call is outside goldthread entirely.
