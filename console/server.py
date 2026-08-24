#!/usr/bin/env python3
"""goldthread console — a live desktop view over real goldthread state.

Reads what the system already keeps on disk and serves it as one JSON
snapshot per view. Deliberately zero-dependency (stdlib only) so it runs
on the system python with nothing to install.

  kanban.db                the durable board (opened READ-ONLY, always)
  projects.db              Hermes' first-class projects -> sidebar views
  pm-ledger.jsonl          pre-summarized lifecycle events from the hooks
  GOLD_THESIS.md           the immutable thesis (per project primary path)
  profiles/*/config.yaml   which model actually runs each role

Views (the sidebar):
  all          every task on the board, thesis via the default resolution
  <slug>       one Hermes project: tasks WHERE project_id = its id, thesis
               from its primary folder
  unfiled      tasks with NO project_id. Exists so a per-project filter can
               never silently hide work — appears only when such tasks do.
  ad-hoc dirs  --project NAME=PATH for a thesis dir that isn't a Hermes
               project (board scope stays empty, and the view says why)

Read-only by design. Writing to kanban.db behind Hermes' back would race
its dispatcher (60s gateway tick) and corrupt state that survives restarts;
any future write action should shell out to `hermes kanban ...` and go
through the same locking every other writer uses.

  python3 server.py [--port 9120] [--project NAME=PATH ...]
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
KANBAN_DB = HERMES_HOME / "kanban.db"
PROJECTS_DB = HERMES_HOME / "projects.db"
PROFILES_DIR = HERMES_HOME / "profiles"

SPOKES = [
    ("gt-pm", "PM", "hub"),
    ("gt-research", "Research", "spoke"),
    ("gt-infra", "Infra / DevOps", "spoke"),
    ("gt-bakeoff", "Bake-Off", "spoke"),
    ("gt-review", "Code Review", "spoke"),
    ("gt-dumbq", "Dumb Question", "spoke"),
]

# Hermes' real status vocabulary (from `hermes kanban list --status`):
# archived, blocked, done, ready, review, running, scheduled, todo, triage.
COLUMNS = [
    ("backlog", "Backlog", ["triage", "todo", "scheduled"]),
    ("ready", "Ready", ["ready"]),
    ("running", "Running", ["running"]),
    ("review", "Review", ["review"]),
    ("blocked", "Blocked", ["blocked"]),
    ("done", "Done", ["done"]),
]
STATUS_TO_COL = {s: key for key, _, sts in COLUMNS for s in sts}


# ── paths ────────────────────────────────────────────────────────────────

def ledger_path() -> Path:
    explicit = os.environ.get("GOLDTHREAD_LEDGER_PATH")
    if explicit:
        return Path(explicit)
    # install.sh writes this into ~/.hermes/.env rather than the environment
    # this process inherits, so fall back to reading it there.
    env_file = HERMES_HOME / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GOLDTHREAD_LEDGER_PATH="):
                return Path(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return Path.home() / ".goldthread" / "pm-ledger.jsonl"


def default_thesis_path() -> Path:
    """Resolution order inject.py uses, so the console and the running
    agents can never disagree about which file is the thesis."""
    explicit = os.environ.get("GOLDTHREAD_THESIS_PATH")
    if explicit:
        return Path(explicit)
    cwd_cand = Path.cwd() / "GOLD_THESIS.md"
    if cwd_cand.exists():
        return cwd_cand
    return HERMES_HOME / "GOLD_THESIS.md"


# ── views (projects) ─────────────────────────────────────────────────────

def load_views(extra_projects):
    """Sidebar views: 'all', each live Hermes project, ad-hoc CLI dirs,
    and 'unfiled' when NULL-project tasks exist (added in build_state)."""
    views = [{"key": "all", "name": "All work", "kind": "all",
              "path": None, "project_id": None}]
    if PROJECTS_DB.exists():
        try:
            con = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True, timeout=3)
            rows = con.execute(
                "SELECT id, slug, name, primary_path FROM projects"
                " WHERE archived = 0 ORDER BY created_at"
            ).fetchall()
            con.close()
            for pid, slug, name, primary in rows:
                views.append({"key": slug, "name": name, "kind": "hermes",
                              "path": primary, "project_id": pid})
        except Exception:
            pass
    for name, path in extra_projects:
        key = "dir-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        views.append({"key": key, "name": name, "kind": "adhoc",
                      "path": path, "project_id": None})
    return views


# ── readers ──────────────────────────────────────────────────────────────

def read_thesis(path: Path | None):
    out = {"path": str(path) if path else None, "exists": False, "mode": None,
           "read_only": False, "core_problem": "", "definition_of_done": [],
           "rules": [], "raw": ""}
    if not path or not path.exists():
        return out
    try:
        out["exists"] = True
        mode = oct(path.stat().st_mode & 0o777)
        out["mode"] = mode
        out["read_only"] = mode in ("0o444", "0o400")
        text = path.read_text(encoding="utf-8")
        out["raw"] = text
        for sec in re.split(r"^##\s+", text, flags=re.M):
            head, _, body = sec.partition("\n")
            h = head.strip().lower()
            if h.startswith("core problem"):
                out["core_problem"] = " ".join(
                    l.strip() for l in body.strip().splitlines() if l.strip())
            elif h.startswith("definition of done"):
                out["definition_of_done"] = [
                    re.sub(r"^[-*]\s*", "", l).strip()
                    for l in body.splitlines() if l.strip().startswith(("-", "*"))]
            elif h.startswith("core rules"):
                for l in body.splitlines():
                    m = re.match(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*", l)
                    if m:
                        out["rules"].append({"n": int(m.group(1)), "title": m.group(2)})
    except Exception as exc:
        out["error"] = str(exc)
    return out


_TASK_COLS = ("id, title, assignee, status, priority, created_at, started_at,"
              " completed_at, block_kind, project_id, last_failure_error, tenant")

# Durable human-only marker. `tenant` is never touched by `specify` (which
# rewrites titles — confirmed live, "Design gossip-heat..." became "Design
# Gossip-Heat Ranking Formula") or by block/unblock (confirmed by reading
# their UPDATE statements in kanban_db.py) — so it survives every mutation
# path that could otherwise strip a marker. A title prefix is kept as a
# fallback ONLY because one real task was already hand-tagged this way
# before `tenant` was chosen as the primary mechanism; new human-only tasks
# should always set --tenant human-only, not rely on the title.
_HUMAN_ONLY_TENANT = "human-only"
_HUMAN_ONLY_TITLE_PREFIX = "[HUMAN-ONLY]"


def is_human_only(task: dict) -> bool:
    return (task.get("tenant") == _HUMAN_ONLY_TENANT
            or str(task.get("title") or "").startswith(_HUMAN_ONLY_TITLE_PREFIX))


def read_tasks():
    """The whole live board in one read-only pass, plus per-project counts
    for the sidebar. Filtering into views happens in memory — one query,
    not one per view."""
    if not KANBAN_DB.exists():
        return [], {}, "kanban.db not found — run `hermes kanban init`"
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT {_TASK_COLS} FROM tasks WHERE status != 'archived'"
            " ORDER BY COALESCE(priority, 999), created_at DESC").fetchall()
        con.close()
        tasks = [dict(r) for r in rows]
        blocked_ids = [t["id"] for t in tasks if t.get("status") == "blocked"]
        reasons = {}
        run_reasons = {}
        reviewed = set()
        if blocked_ids:
            con2 = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=3)
            q = ("SELECT task_id, body FROM task_comments WHERE body LIKE 'BLOCKED:%'"
                 f" AND task_id IN ({','.join('?' * len(blocked_ids))}) ORDER BY id")
            for tid, body in con2.execute(q, blocked_ids):
                reasons[tid] = body[len("BLOCKED:"):].strip()[:200]
            # The REAL reason a crash/give-up blocked a task lives in
            # task_runs.error + .outcome, not tasks.last_failure_error (which
            # was empty on every crash I checked). The diagnostic that found
            # this initially mis-read it as "Hermes records no reason" — it
            # does, in a column the console wasn't reading. Take the latest
            # run's outcome+error for each blocked task.
            # Only FAILURE-outcome runs explain a block. A task can crash,
            # get re-dispatched, and later complete; "last run wins" would
            # then show 'completed' for a task that's blocked. Restrict to
            # the failure outcomes and take the latest of those.
            fails = ("crashed", "timed_out", "gave_up", "spawn_failed", "failed")
            rq = ("SELECT task_id, outcome, error FROM task_runs"
                  f" WHERE task_id IN ({','.join('?' * len(blocked_ids))})"
                  f" AND outcome IN ({','.join('?' * len(fails))}) ORDER BY id")
            for tid, outcome, error in con2.execute(rq, list(blocked_ids) + list(fails)):
                run_reasons[tid] = (outcome, (error or "")[:200])  # latest failure wins
            con2.close()
        # Which done tasks have a linked review task (task_links child)?
        done_ids = [t["id"] for t in tasks if t.get("status") == "done"]
        if done_ids:
            con3 = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=3)
            lq = (f"SELECT parent_id FROM task_links WHERE parent_id IN "
                  f"({','.join('?' * len(done_ids))})")
            for (pid,) in con3.execute(lq, done_ids):
                reviewed.add(pid)
            con3.close()
        counts = {}
        # Computed once, not per task: which profiles are local right now.
        # Cheap (six small file reads), but there's no reason to redo it for
        # every task in the loop below.
        local_profiles = {n for n, _, _ in SPOKES
                          if (read_profile_model(n).get("provider") or "").startswith("ollama")}
        for t in tasks:
            t["column"] = STATUS_TO_COL.get(t.get("status"), "backlog")
            # Reason precedence: an explicit human "BLOCKED:" comment, then
            # the run's own recorded outcome/error, then last_failure_error.
            rr = run_reasons.get(t["id"])
            run_txt = None
            if rr:
                outcome, err = rr
                run_txt = (f"{outcome}: {err}" if outcome and err else (err or outcome))
            t["block_reason"] = reasons.get(t["id"]) or run_txt or t.get("last_failure_error")
            t["reviewed"] = t["id"] in reviewed
            t["human_only"] = is_human_only(t)
            # A real FAILURE-outcome run (not just any BLOCKED comment) on a
            # profile that's currently local-primary — exactly the case
            # escalate_task exists for. rr (not block_reason) is checked
            # deliberately: block_reason also fires on a human "BLOCKED:"
            # note with no failed run behind it, which cloud can't fix.
            t["local_escalatable"] = bool(
                rr and t.get("status") == "blocked" and t.get("assignee") in local_profiles)
            counts[t.get("project_id")] = counts.get(t.get("project_id"), 0) + 1
        return tasks, counts, None
    except Exception as exc:
        return [], {}, f"kanban.db unreadable: {exc}"


_LEDGER_TAIL_BYTES = 256 * 1024  # bounded read: the ledger only grows


def read_ledger(hours=24, limit=40):
    path = ledger_path()
    counts = {"claimed": 0, "completed": 0, "blocked": 0}
    if not path.exists():
        return {"path": str(path), "exists": False, "counts": counts,
                "recent": [], "note": "No lifecycle events yet — the ledger is "
                                      "created the first time a kanban task is "
                                      "claimed, completed, or blocked."}
    cutoff = time.time() - hours * 3600
    recent = []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > _LEDGER_TAIL_BYTES:
                fh.seek(size - _LEDGER_TAIL_BYTES)
                fh.readline()  # drop the partial line we landed inside
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("ts", 0) < cutoff:
                    continue
                ev = rec.get("event")
                if ev in counts:
                    counts[ev] += 1
                recent.append(rec)
    except Exception as exc:
        return {"path": str(path), "exists": True, "counts": counts,
                "recent": [], "note": f"unreadable: {exc}"}
    return {"path": str(path), "exists": True, "counts": counts,
            "recent": recent[-limit:]}


# `[ \t]*$` not `\s*$`: \s matches newlines, which lets the pattern swallow
# the line break and mis-anchor the indented block that follows.
_MODEL_BLOCK = re.compile(r"^model:[ \t]*$((?:\n[ \t]+.*)*)", re.M)


def read_profile_model(profile):
    """Pull `model.provider` / `model.default` out of a profile's config.yaml.
    Hand-parsed rather than pyyaml (not in the system python) or shelling out
    to `hermes config get` (~1-2s startup per profile per refresh)."""
    cfg = PROFILES_DIR / profile / "config.yaml"
    out = {"provider": None, "model": None, "configured": False}
    if not cfg.exists():
        return out
    try:
        m = _MODEL_BLOCK.search(cfg.read_text(encoding="utf-8"))
        if not m:
            return out
        block = m.group(1)
        prov = re.search(r"^[ \t]+provider:\s*(.+)$", block, re.M)
        default = re.search(r"^[ \t]+default:\s*(.+)$", block, re.M)
        if prov:
            out["provider"] = prov.group(1).strip().strip("'\"")
        if default:
            out["model"] = default.group(1).strip().strip("'\"")
        out["configured"] = bool(out["model"])
    except Exception:
        pass
    return out


# ── fix 5: pre-dispatch health checks ───────────────────────────────────
# Built directly from a real incident: `hermes kanban specify` died with
# AuthenticationError because a stale ANTHROPIC_API_KEY in the calling
# shell's environment shadowed a working, "logged in" OAuth credential —
# Hermes treats an explicit env var as higher-priority "user intent" than
# stored auth, regardless of whether that env var is any good. `hermes
# auth status` alone would NOT have caught this: it only reports whether a
# credential is stored, not whether something in the environment overrides
# it. Checking for the exact env var that bit me is what makes this a real
# pre-dispatch check instead of a restatement of "auth status: logged in".

# Provider -> the env var(s) Hermes' resolver treats as higher-priority
# than a stored OAuth/API-key credential for that provider. Extend this
# map if more providers get wired to real profiles later.
_SHADOW_RISK_ENV = {"anthropic": ["ANTHROPIC_API_KEY"]}


def check_health(profile):
    """Cheap, real reachability check for one profile's configured model —
    NOT a full auth verification (that costs a real API call), but the
    specific failure mode that actually happened here."""
    mdl = read_profile_model(profile)
    out = {"profile": profile, "provider": mdl["provider"], "model": mdl["model"],
           "configured": mdl["configured"], "ok": None, "detail": None}
    if not mdl["configured"]:
        out["detail"] = "no model configured for this profile"
        return out

    if (mdl["provider"] or "").startswith("ollama"):
        base = "http://127.0.0.1:11434"
        try:
            req = urllib.request.Request(f"{base}/api/tags")
            with urllib.request.urlopen(req, timeout=4) as r:
                tags = json.loads(r.read()).get("models", [])
            names = {t.get("name") for t in tags} | {(t.get("name") or "").split(":")[0] for t in tags}
            if mdl["model"] in names:
                out["ok"] = True
                out["detail"] = f"ollama reachable, {mdl['model']} present"
            else:
                out["ok"] = False
                out["detail"] = (f"ollama reachable, but {mdl['model']!r} is not pulled "
                                 f"— dispatch would fail immediately. "
                                 f"`ollama pull {mdl['model']}`")
        except Exception as exc:
            out["ok"] = False
            out["detail"] = f"ollama unreachable at {base}: {exc}"
        return out

    # Cloud: check stored credential presence, then the specific shadowing
    # risk that actually caused a real failure. Scoped with -p so this
    # reflects the PROFILE's own credential pool, not just the global one.
    try:
        argv = ["hermes", "-p", profile, "auth", "status"]
        if mdl["provider"]:  # an empty positional arg is a CLI error, not "all"
            argv.append(mdl["provider"])
        r = subprocess.run(argv, capture_output=True, text=True, timeout=6,
                           env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
        logged_in = "logged in" in (r.stdout or "").lower()
    except Exception as exc:
        out["detail"] = f"auth status check failed: {exc}"
        return out
    shadow_vars = [v for v in _SHADOW_RISK_ENV.get(mdl["provider"] or "", []) if os.environ.get(v)]
    if not logged_in:
        out["ok"] = False
        out["detail"] = f"{mdl['provider']}: not logged in — hermes -p {profile} model"
    elif shadow_vars:
        out["ok"] = False
        out["detail"] = (
            f"{mdl['provider']}: logged in, BUT {', '.join(shadow_vars)} is set in this "
            f"console's environment and Hermes prefers an explicit env var over stored "
            f"auth — if it's stale, dispatch fails with AuthenticationError (this exact "
            f"thing happened once already). Note: this only reflects the CONSOLE's own "
            f"environment, not necessarily the gateway's — a clean gateway process may "
            f"be unaffected even if this warns.")
    else:
        out["ok"] = True
        out["detail"] = f"{mdl['provider']}: logged in, no known shadow-risk env vars set"
    return out


# ── fix 7: model picker for early/exploratory work ──────────────────────
# `hermes kanban create --model X --provider Y` pins ONE task to a model
# without touching the assignee's profile config — exactly what "try this
# on a different model before committing" wants. But it only resolves if
# the assignee's profile has that provider defined at all. Until
# 2026-08-24 only gt-pm and gt-dumbq carried a `providers.ollama-local`
# block — gt-research/infra/bakeoff/review ran as a cloud stopgap instead,
# explicitly called "not a permanent architecture decision" at the time.
# Reverted: local is the default for all six profiles again, cloud is now
# an escalation path (see escalate_task below), not a parallel default.
# Offering a local-model override for a profile that doesn't have this
# block would be a picker that looks real and fails at dispatch time, so
# this stays a real, verified set rather than "every profile."
_LOCAL_OVERRIDE_PROVIDER = "ollama-local"
_LOCAL_OVERRIDE_PROFILES = {"gt-pm", "gt-research", "gt-infra", "gt-bakeoff",
                            "gt-review", "gt-dumbq"}  # verified via config.yaml, not guessed


def list_local_models():
    """Every model actually pulled in ollama, for the create-task picker.
    Deliberately not scoped to the two local profiles' OWN configured
    model — the point of a picker is trying one you're NOT already using."""
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=4) as r:
            tags = json.loads(r.read()).get("models", [])
        return sorted({t.get("name") for t in tags if t.get("name")})
    except Exception:
        return []


def _local_model_hint(name):
    """Guidance shown next to a local model in the create-task picker —
    the user's own stated preference (2026-08-24), not a benchmark result:
    qwen for code-shaped work, mistral for text-shaped work. Left blank for
    anything else rather than guessing at models with no stated guidance."""
    n = name.lower()
    if "qwen" in n:
        return "better for coding"
    if "mistral" in n:
        return "better for text"
    return ""


_KANBAN_KEYS = ("dispatch_in_gateway", "auto_decompose", "review_dispatch",
                "dispatch_interval_seconds")


def read_kanban_config():
    """Hermes' kanban automation settings — the ones that decide whether a
    filed task actually stays put.

    This matters more than it looks. `auto_decompose` (default TRUE, 3 per
    dispatcher tick) grooms `triage` tasks and PROMOTES them to `ready`,
    where the dispatcher claims them. So filing to Backlog does NOT hold
    work: with the stock config, anything you file is groomed and dispatched
    within a couple of minutes. The console must say so rather than imply a
    safety it does not have.
    """
    out = {k: None for k in _KANBAN_KEYS}
    defaults = {"dispatch_in_gateway": True, "auto_decompose": True,
                "review_dispatch": True, "dispatch_interval_seconds": 60}
    cfg = HERMES_HOME / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8")
        m = re.search(r"^kanban:[ \t]*$((?:\n[ \t]+.*)*)", text, re.M)
        block = m.group(1) if m else ""
        for k in _KANBAN_KEYS:
            km = re.search(rf"^[ \t]+{k}:\s*(.+)$", block, re.M)
            if km:
                v = km.group(1).strip().strip("'\"")
                out[k] = (v.lower() == "true") if v.lower() in ("true", "false") else v
    except Exception:
        pass
    for k, d in defaults.items():
        if out[k] is None:
            out[k] = d
            out[k + "_source"] = "default"
        else:
            out[k + "_source"] = "config.yaml"
    try:
        out["dispatch_interval_seconds"] = int(out["dispatch_interval_seconds"])
    except Exception:
        out["dispatch_interval_seconds"] = 60
    return out


_GW_CACHE = {"at": 0.0, "val": None}
_GW_TTL = 5.0


def gateway_running():
    """The dispatcher lives in the gateway; without it, ready tasks never
    get picked up. Cached: every open console tab polls this endpoint, and
    a pgrep subprocess per poll per tab adds up."""
    now = time.monotonic()
    if now - _GW_CACHE["at"] < _GW_TTL:
        return _GW_CACHE["val"]
    try:
        r = subprocess.run(
            ["pgrep", "-f", "hermes gateway|hermes_cli.main gateway"],
            capture_output=True, text=True, timeout=3)
        val = r.returncode == 0
    except Exception:
        val = None
    _GW_CACHE.update(at=now, val=val)
    return val


# ── goldthread coverage (fix 9: catch the "enabled in default only" trap) ─
# Named profiles discover plugins from their OWN plugins/ dir, so goldthread
# has to be enabled in every spoke — not just the default profile. When it
# isn't, the guard (pre_tool_call), thesis re-injection (pre_llm_call), and
# the completed/blocked ledger hooks (which fire in the WORKER process) are
# all silently inert for that spoke: work runs unguarded and completions
# never reach the PM ledger. This bit a real run — the ledger recorded 27
# claims and 3 completions across 28 finished tasks. It's invisible unless
# something looks, so the console looks: a cheap filesystem check per spoke,
# surfaced as a hard alert. Presence of the plugin dir/symlink is the proxy;
# a stronger `hermes -p X plugins list` check would cost ~1-2s per profile.

def goldthread_coverage():
    """Which spoke profiles actually have goldthread wired in. Cheap: just
    checks for the plugin under each profile's own plugins/ dir."""
    out = {"covered": [], "uncovered": []}
    for name, _label, _kind in SPOKES:
        p = PROFILES_DIR / name / "plugins" / "goldthread"
        # a symlink counts even if broken-at-a-glance; exists() follows it
        (out["covered"] if (p.exists() or p.is_symlink()) else out["uncovered"]).append(name)
    return out


# ── artifacts (fix 3: done tickets should show their work) ─────────────
# A completed task's worktree is deleted on completion (confirmed: the
# gossip-heat task's .worktrees/<id> dir was gone within a poll cycle of
# `done`), but its branch survives in the project's primary repo. That
# branch — not kanban.db — is where the actual deliverable lives, and
# nothing in the board surfaced it; finding it required going into git by
# hand. `git log`/`git diff` only, read-only, matching the same posture as
# the rest of this server.

def _project_primary_path(project_id):
    if not project_id or not PROJECTS_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True, timeout=3)
        row = con.execute("SELECT primary_path FROM projects WHERE id = ?",
                          (project_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _git(repo, *args, timeout=5):
    try:
        r = subprocess.run(["git", "-C", repo] + list(args),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return False, "", str(exc)


def _read_run_cost(profile, session_id, timeout=8):
    """Fix 4: cost and attribution. `worker_session_id` lives in each run's
    `metadata` JSON (found by inspecting a real completed run — kanban.db
    itself has no cost/token columns anywhere). `hermes sessions export`
    lives under the WORKER's own profile home, not the console's — a bare
    export without -p returned "not found" for a real gt-research session;
    scoping to the profile is what made it resolve.

    Deliberately not called on every /api/state poll: this shells out and
    took real wall-clock in testing (~1-2s), which is fine once per click,
    not once per ticket per 5s tick across every open window.
    """
    if not profile or not session_id or not _NAME_RE.match(profile):
        return None
    try:
        r = subprocess.run(
            ["hermes", "-p", profile, "sessions", "export",
             "--session-id", session_id, "--format", "jsonl", "-"],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
        if r.returncode != 0 or not r.stdout.strip():
            return None
        d = json.loads(r.stdout.strip().splitlines()[0])
    except Exception:
        return None
    cost = d.get("actual_cost_usd")
    is_estimate = cost is None
    if cost is None:
        cost = d.get("estimated_cost_usd")
    # cost_status distinguishes a REAL zero/estimate from "Hermes has no
    # price-table entry for this model" — confirmed live, running a task
    # pinned to claude-fable-5 (and one specific Haiku snapshot id) via the
    # router's --model override: both came back cost_status="unknown",
    # cost_source="none", estimated_cost_usd=0.0, on a run with 54K output
    # tokens and 177K cache-write tokens — nowhere near actually free. A
    # priced model (sonnet) on the same project showed cost_status
    # "estimated" with a real source. Silently showing "$0.00" for an
    # unpriced run is indistinguishable from a genuinely free one and
    # defeats the entire point of a cost rollup.
    priced = d.get("cost_status") != "unknown"  # missing field (older Hermes) defaults to priced
    return {
        "model": d.get("model"), "api_calls": d.get("api_call_count"),
        "input_tokens": d.get("input_tokens"), "output_tokens": d.get("output_tokens"),
        "cache_read_tokens": d.get("cache_read_tokens"),
        "cost_usd": round(cost, 4) if isinstance(cost, (int, float)) else cost,
        "cost_is_estimate": is_estimate,
        "cost_priced": priced,
    }


def read_artifact(task_id):
    """What a task's run(s) actually did: cost/attribution (fix 4) and, if
    it committed anything, the branch content (fix 3). Read-only throughout
    — git log/show/diff/merge-base, and `hermes sessions export`, never a
    mutation."""
    out = {"task_id": task_id, "has_repo": False, "branch": None,
           "exists": False, "commits": [], "diff_stat": None, "note": None,
           "runs": []}
    if not _ID_RE.match(str(task_id or "")):
        out["note"] = "invalid task id"
        return out
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=3)
        row = con.execute(
            "SELECT branch_name, project_id, workspace_kind FROM tasks WHERE id = ?",
            (task_id,)).fetchone()
        runs = con.execute(
            "SELECT profile, status, started_at, ended_at, metadata FROM task_runs"
            " WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        con.close()
    except Exception as exc:
        out["note"] = f"kanban.db unreadable: {exc}"
        return out
    if not row:
        out["note"] = "task not found"
        return out
    branch, project_id, workspace_kind = row

    for profile, status, started_at, ended_at, metadata_raw in runs:
        run_out = {"profile": profile, "status": status,
                   "duration_s": (round(ended_at - started_at) if started_at and ended_at else None),
                   "cost": None}
        try:
            meta = json.loads(metadata_raw) if metadata_raw else {}
        except Exception:
            meta = {}
        sid = meta.get("worker_session_id")
        if sid:
            run_out["cost"] = _read_run_cost(profile, sid)
        out["runs"].append(run_out)

    out["branch"] = branch
    if workspace_kind != "worktree" or not branch:
        out["note"] = "this task's workspace isn't a git worktree — no branch to show"
        return out
    repo = _project_primary_path(project_id)
    if not repo or not Path(repo).is_dir():
        out["note"] = "project has no resolvable primary folder"
        return out
    ok, _, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    if not ok:
        out["note"] = "primary folder isn't a git repo (no artifact possible until it is)"
        return out
    out["has_repo"] = True
    ok, _, _ = _git(repo, "rev-parse", "--verify", branch)
    if not ok:
        out["note"] = f"branch '{branch}' not found — the task may not have committed anything"
        return out
    out["exists"] = True
    ok, base, _ = _git(repo, "merge-base", "HEAD", branch)
    base = base or "HEAD"
    ok, log, _ = _git(repo, "log", f"{base}..{branch}",
                      "--format=%H%x1f%an%x1f%at%x1f%s", "-20")
    if ok and log:
        for line in log.split("\n"):
            parts = line.split("\x1f")
            if len(parts) == 4:
                out["commits"].append({"sha": parts[0][:10], "author": parts[1],
                                       "at": int(parts[2]), "subject": parts[3]})
    ok, stat, _ = _git(repo, "diff", "--stat", f"{base}...{branch}")
    if ok:
        out["diff_stat"] = stat
    return out


# ── design studio (fix 8: a real design/UI surface) ────────────────────
# A project's UI work lands as self-contained HTML in <primary>/design/,
# and its tokens are seeded in docs/design/design-system.md. Nothing in the
# console showed either — you had to open the file by hand to see what a
# design task produced, and the token set lived only as prose. This reads
# both: the artifacts (served into sandboxed preview frames) and the REAL
# token values, parsed from the artifact's own `:root` block rather than
# the prose table, because the CSS is what actually renders. The prose
# table only supplies the human "what's it for" description per token.

_DESIGN_SUBDIR = "design"
_DESIGN_SYSTEM_DOC = os.path.join("docs", "design", "design-system.md")
# design/ was the only convention the studio ever scanned, and it only held
# because Cat Gossip's and Atelier's task PROMPTS explicitly said "build
# design/X.html". Nothing enforces that on auto-generated work — running
# PourRate through the Model Router (no such instruction in its subtask
# prompts) produced real UI in web/composer.html, completely invisible to
# the studio. web/ and public/ are the other two conventions actually
# observed; design/ stays first so existing projects keep their priority.
_UI_HTML_DIRS = ("design", "web", "public")
# Non-HTML UI source the studio CANNOT render — no browser executes raw
# .tsx/.jsx/.vue/.swift/.kt without a build step goldthread doesn't run
# (confirmed: PourRate's age gate is a real SwiftUI package, its composer a
# React/TS frontend — router subtasks landed in totally different stacks
# with nothing to preview either one). Can't preview these, but a pointer
# list beats total silence about UI work existing.
_OTHER_UI_SRC_EXTS = (".tsx", ".jsx", ".vue", ".swift", ".kt")
_OTHER_UI_SRC_DIRS = ("src", "Sources", "app")
_OTHER_UI_SRC_CAP = 20  # a pointer list, not a file browser
_ROOT_BLOCK_RE = re.compile(r":root\s*\{([^}]*)\}", re.S)
_CSS_VAR_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
_COLORISH_RE = re.compile(r"^(#[0-9A-Fa-f]{3,8}|rgb|rgba|hsl|hsla)\b", re.I)
_MD_COLOR_ROW_RE = re.compile(
    r"\|\s*`?(--[\w-]+)`?\s*\|\s*`?(#[0-9A-Fa-f]{3,8})`?\s*\|\s*(.+?)\s*\|")
_MD_FONTSTACK_RE = re.compile(r"[Ff]ont stack:\s*`([^`]+)`")


def _parse_root_tokens(css_text):
    """Ordered [(name, value)] from the FIRST :root block — the authoritative
    implemented token set (design-system.md itself says to copy this block)."""
    m = _ROOT_BLOCK_RE.search(css_text or "")
    if not m:
        return []
    out = []
    for name, value in _CSS_VAR_RE.findall(m.group(1)):
        out.append((name, value.strip()))
    return out


_VAR_REF_RE = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)")


def _resolve_css_var(value, tokens, depth=3):
    """Resolve `var(--x)` (and its fallback arg) against parsed tokens.

    Bounded depth because CSS custom properties can chain, and a malformed
    or self-referential token set would otherwise spin forever.
    """
    if not value:
        return value
    by_name = {t["name"]: t["value"] for t in tokens}
    seen = set()
    for _ in range(depth):
        m = _VAR_REF_RE.search(value)
        if not m:
            break
        name, fallback = m.group(1), (m.group(2) or "").strip()
        if name in seen:
            break
        seen.add(name)
        replacement = by_name.get(name, fallback)
        if replacement is None:
            break
        value = (value[:m.start()] + replacement + value[m.end():]).strip()
    return value


def _classify_token(name, value):
    n = name.lower()
    if _COLORISH_RE.match(value):
        return "color"
    if "radius" in n:
        return "radius"
    if "space" in n or "spacing" in n or "gap" in n:
        return "space"
    if "shadow" in n:
        return "shadow"
    if "font" in n:
        return "font"
    return "other"


def _scan_html_artifacts(primary):
    """*.html one level inside each of _UI_HTML_DIRS, no descent, no
    symlinks out. Sorted within each dir, dirs in priority order, capped —
    a preview surface, not a file browser. Each hit is tagged with the
    directory it came from so the UI can show which convention a given
    project actually used, rather than assuming they're all design/."""
    out = []
    for sub in _UI_HTML_DIRS:
        d = os.path.join(primary, sub)
        try:
            for entry in sorted(os.listdir(d)):
                if not entry.lower().endswith((".html", ".htm")):
                    continue
                full = os.path.join(d, entry)
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                out.append({"dir": sub, "file": entry, "path": full})
        except Exception:
            continue
    return out[:12]


def _scan_other_ui_source(primary):
    """Real UI source the studio can't render — see _OTHER_UI_SRC_EXTS.
    Bounded walk (2 levels deep under each root, capped file count) so a
    huge node_modules-adjacent tree can't turn this into a slow file
    browser; it's meant to answer 'does UI work exist here at all', not
    enumerate a project."""
    out = []
    for sub in _OTHER_UI_SRC_DIRS:
        root = os.path.join(primary, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath[len(root):].count(os.sep) >= 2:
                dirnames[:] = []  # don't descend further
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and d not in ("node_modules", "__pycache__", ".build")]
            for f in sorted(filenames):
                if f.lower().endswith(_OTHER_UI_SRC_EXTS):
                    out.append(os.path.relpath(os.path.join(dirpath, f), primary))
                    if len(out) >= _OTHER_UI_SRC_CAP:
                        return out
    return out


_DESIGN_FILE_CAP = 600_000  # bytes; a preview frame, not a payload channel


def read_design(view_key, extra_projects):
    """The design surface for one view: real artifacts + real tokens."""
    views = load_views(extra_projects)
    view = next((v for v in views if v["key"] == view_key), None)
    out = {"view": view_key, "name": view["name"] if view else view_key,
           "path": (view or {}).get("path"), "artifacts": [], "tokens": [],
           "font_stack": None, "notes": None, "has_system": False, "note": None,
           "thesis_rules": [], "other_ui_source": []}
    primary = (view or {}).get("path")
    if not primary or not os.path.isdir(primary):
        out["note"] = ("This view has no project folder on disk, so there are "
                       "no design artifacts to show.")
        return out

    # The project's own thesis Core Rules, so the studio can double as a
    # human review surface: check each rule against the live frame while you
    # look at it (the board's 'thesis review' button dispatches an AI review;
    # this is the eyes-on-the-artifact counterpart).
    out["thesis_rules"] = read_thesis(Path(primary) / "GOLD_THESIS.md")["rules"]

    files = _scan_html_artifacts(primary)
    out["other_ui_source"] = _scan_other_ui_source(primary)

    # Token descriptions from the prose table (name -> "use"), if present.
    md_uses = {}
    doc_path = os.path.join(primary, _DESIGN_SYSTEM_DOC)
    try:
        if os.path.isfile(doc_path):
            md = Path(doc_path).read_text(encoding="utf-8")
            out["has_system"] = True
            for name, _hex, use in _MD_COLOR_ROW_RE.findall(md):
                md_uses[name] = use.strip()
            fm = _MD_FONTSTACK_RE.search(md)
            if fm:
                out["font_stack"] = fm.group(1).strip()
            # First paragraph after the "Tone:" marker, if any, as a note.
            tm = re.search(r"[Tt]one:\s*(.+)", md)
            if tm:
                out["notes"] = tm.group(1).strip()[:280]
    except Exception:
        pass

    root_source = None
    for hit in files:
        name, full, srcdir = hit["file"], hit["path"], hit["dir"]
        try:
            size = os.path.getsize(full)
            text = Path(full).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            out["artifacts"].append({"file": name, "dir": srcdir, "error": str(exc)})
            continue
        art = {"file": name, "dir": srcdir, "bytes": size,
               "truncated": size > _DESIGN_FILE_CAP,
               "html": text[:_DESIGN_FILE_CAP]}
        try:
            art["mtime"] = int(os.path.getmtime(full))
        except Exception:
            art["mtime"] = None
        out["artifacts"].append(art)
        if root_source is None and _ROOT_BLOCK_RE.search(text):
            root_source = (name, text)

    if root_source:
        src_name, css = root_source
        out["token_source"] = src_name
        for tname, tval in _parse_root_tokens(css):
            out["tokens"].append({
                "name": tname, "value": tval,
                "kind": _classify_token(tname, tval),
                "use": md_uses.get(tname),
            })
    if not out["font_stack"]:
        # Fall back to the artifact's own body font-family if the doc didn't
        # name one — still real, just less curated.
        for art in out["artifacts"]:
            fm = re.search(r"font-family:\s*([^;]+);", art.get("html", ""))
            if fm:
                out["font_stack"] = fm.group(1).strip()
                break
    # ...but a well-tokenised artifact writes `font-family: var(--font-ui)`,
    # so that fallback hands back a token REFERENCE, not a font stack — the
    # type specimens then render in an unresolved font. Resolve one level of
    # var() against the tokens we just parsed. Found by the Atelier run,
    # whose design system tokenises fonts; Cat Gossip's hardcodes them,
    # which is why this stayed hidden the first time.
    out["font_stack"] = _resolve_css_var(out["font_stack"], out["tokens"])
    if not out["font_stack"]:
        for t in out["tokens"]:
            if t["kind"] == "font" and "mono" not in t["name"]:
                out["font_stack"] = t["value"]
                break

    if not files:
        if out["other_ui_source"]:
            # Real UI work exists — just not in a form a browser can run
            # without a build step (React/Vue/Swift/Kotlin source). Saying
            # "no design artifacts yet" here would be actively wrong, not
            # just unhelpful — this is the case that was totally silent
            # before: PourRate's SwiftUI age gate and React composer.
            shown = out["other_ui_source"][:8]
            more = len(out["other_ui_source"]) - len(shown)
            out["note"] = (
                "No previewable HTML — the studio can only render "
                "self-contained HTML (checked design/, web/, public/). Real "
                "UI source exists, just not in a runnable-in-a-browser form: "
                + ", ".join(shown) + (f", and {more} more" if more > 0 else "")
                + ". Open these directly to review them.")
        else:
            out["note"] = (
                f"No design artifacts yet. UI work lands as self-contained "
                f"HTML in {view['name'] if view else ''}/design/ (or web/, "
                f"public/) — file a design task and it'll appear here.")
    return out


# ── state ────────────────────────────────────────────────────────────────

def build_state(view_key, extra_projects):
    views = load_views(extra_projects)
    tasks, proj_counts, board_err = read_tasks()

    unfiled_count = proj_counts.get(None, 0)
    if unfiled_count:
        views.append({"key": "unfiled", "name": "Unfiled", "kind": "unfiled",
                      "path": None, "project_id": None})

    view = next((v for v in views if v["key"] == view_key), views[0])

    # scope tasks to the view
    if view["kind"] == "hermes":
        scoped = [t for t in tasks if t.get("project_id") == view["project_id"]]
        board_note = None
    elif view["kind"] == "unfiled":
        scoped = [t for t in tasks if not t.get("project_id")]
        board_note = ("These tasks have no project. File future ones with "
                      "`hermes kanban create ... --project <slug>`.")
    elif view["kind"] == "adhoc":
        scoped = []
        board_note = ("This directory isn't a Hermes project, so no board tasks "
                      "can be scoped to it. Make it one: `hermes project create "
                      f"\"{view['name']}\" {view['path']}`.")
    else:  # all
        scoped = tasks
        board_note = None

    # thesis for the view
    if view["kind"] in ("hermes", "adhoc") and view["path"]:
        thesis = read_thesis(Path(view["path"]) / "GOLD_THESIS.md")
    else:
        thesis = read_thesis(default_thesis_path())

    by_col = {key: [] for key, _, _ in COLUMNS}
    by_assignee = {}
    for t in scoped:
        by_col.setdefault(t["column"], []).append(t)
        by_assignee.setdefault(t.get("assignee") or "unassigned", []).append(t)

    profiles = []
    for name, label, kind in SPOKES:
        mdl = read_profile_model(name)
        mine = by_assignee.get(name, [])
        profiles.append({
            "name": name, "label": label, "kind": kind,
            "provider": mdl["provider"], "model": mdl["model"],
            "configured": mdl["configured"],
            "local": (mdl["provider"] or "").startswith("ollama"),
            "task_count": len(mine),
            "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"]}
                      for t in mine[:12]],
        })

    # sidebar entries with live counts
    sidebar = []
    for v in views:
        if v["kind"] == "hermes":
            n = proj_counts.get(v["project_id"], 0)
        elif v["kind"] == "all":
            n = len(tasks)
        elif v["kind"] == "unfiled":
            n = unfiled_count
        else:
            n = 0
        sidebar.append({"key": v["key"], "name": v["name"], "kind": v["kind"],
                        "path": v["path"], "count": n,
                        "selected": v["key"] == view["key"]})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hermes_home": str(HERMES_HOME),
        "view": {"key": view["key"], "name": view["name"], "kind": view["kind"],
                 "path": view["path"], "board_note": board_note},
        "views": sidebar,
        "thesis": thesis,
        "profiles": profiles,
        "board": {
            "columns": [{"key": k, "label": lbl, "statuses": sts,
                         "tasks": by_col.get(k, [])} for k, lbl, sts in COLUMNS],
            "total": len(scoped),
            "board_total": len(tasks),
            "error": board_err,
        },
        "ledger": read_ledger(),
        "health": {
            "kanban_db": KANBAN_DB.exists(),
            "gateway_running": gateway_running(),
            "unassigned": len(by_assignee.get("unassigned", [])),
            "kanban_config": read_kanban_config(),
            "goldthread_coverage": goldthread_coverage(),
        },
    }


# ── kanban write layer ───────────────────────────────────────────────────
# The console edits the board by shelling out to `hermes kanban ...`, never
# by writing kanban.db: the dispatcher writes that database on a gateway
# tick, and a second writer would race it. The CLI goes through the same
# locking every other writer uses.
#
# The thesis has NO write path here, deliberately. Amendments are
# chmod 644 -> edit -> chmod 444 -> git commit, by a human, at a keyboard.

class HumanOnlyError(Exception):
    """Refused: the target task is marked human-only.

    This is enforcement at the CONSOLE's write boundary — every action this
    server can take on kanban.db goes through kanban_argv()/do_POST(), so a
    check here is real, not decorative. It is NOT a Hermes-level guarantee:
    the raw `hermes kanban` CLI and the desktop dashboard both bypass it
    entirely, because Hermes has no first-class "never dispatch this, even
    across unblock" concept — `block_kind=needs_input` is not special-cased
    against unblock in the dispatcher (confirmed by reading block_task's own
    UPDATE statements: unblocking a needs_input task returns it to the exact
    same claimable pool as a transient block). A real fix needs an upstream
    Hermes change; this is the honest ceiling of what a read-mostly console
    with a CLI-shelling write layer can guarantee on its own.
    """


_ID_RE = re.compile(r"^t_[0-9a-f]{4,32}$")
_NAME_RE = re.compile(r"^[\w.-]{1,64}$")
_MODEL_RE = re.compile(r"^[\w.:-]{1,80}$")   # ollama tags use ':' (qwen3.5:9b)
_BLOCK_KINDS = {"capability", "dependency", "needs_input", "transient"}


def _short(s, n):
    s = str(s or "").strip()
    return s[:n]


def kanban_argv(body):
    """Translate a validated action into a `hermes kanban` argv, or raise
    ValueError. Strict allowlist: anything not built here cannot run."""
    action = body.get("action")

    def tid():
        t = str(body.get("task_id") or "")
        if not _ID_RE.match(t):
            raise ValueError("bad task id")
        return t

    if action == "create":
        title = _short(body.get("title"), 200)
        if not title:
            raise ValueError("title required")
        argv = ["create", title]
        prompt_body = _short(body.get("body"), 4000)
        if prompt_body:
            argv += ["--body", prompt_body]
        assignee = str(body.get("assignee") or "")
        if assignee:
            if not _NAME_RE.match(assignee):
                raise ValueError("bad assignee")
            argv += ["--assignee", assignee]
        project = str(body.get("project") or "")
        if project:
            if not _NAME_RE.match(project):
                raise ValueError("bad project")
            argv += ["--project", project]
        model = str(body.get("model") or "")
        if model:
            # See the block comment above _LOCAL_OVERRIDE_PROFILES: only
            # gt-pm and gt-dumbq have providers.ollama-local configured, so
            # an override paired with any other assignee resolves to a
            # provider that profile's config doesn't define. Shape-checked
            # here like assignee/project elsewhere in this function; the
            # real membership check (which models are actually pulled)
            # happens client-side by only offering real ones in the <select>
            # — this is defense in depth against a forged request, not the
            # primary guard.
            if not _MODEL_RE.match(model):
                raise ValueError("bad model")
            if assignee and assignee not in _LOCAL_OVERRIDE_PROFILES:
                raise ValueError(
                    f"{assignee} has no local-model override configured "
                    f"(only {', '.join(sorted(_LOCAL_OVERRIDE_PROFILES))} do)")
            argv += ["--model", model, "--provider", _LOCAL_OVERRIDE_PROVIDER]
        prio = body.get("priority")
        if prio is not None:
            if not (isinstance(prio, int) and 1 <= prio <= 9):
                raise ValueError("bad priority")
            argv += ["--priority", str(prio)]
        if body.get("human_only"):
            # Durable marker (see is_human_only), filed straight to BLOCKED
            # rather than triage — deliberately skips the triage resting
            # state entirely. auto_decompose only grooms triage-status
            # tasks; a task that starts blocked is outside its reach even
            # if a user re-enables that setting later. Overrides "dispatch
            # now" unconditionally.
            argv += ["--tenant", _HUMAN_ONLY_TENANT, "--initial-status", "blocked"]
        elif not body.get("dispatch"):
            # SAFE BY DEFAULT: hold in triage unless the caller explicitly
            # opts into dispatch. This used to be inverted — the server added
            # --triage only when the client SENT triage:true, so a request
            # that merely omitted the field filed straight to `ready`, where
            # the gateway claims it and spawns a real (billable) agent within
            # about a minute. The safe default therefore lived only in the
            # browser, one forgotten field away from starting an agent
            # nobody asked for. Found by POSTing to /api/kanban directly.
            # `triage` is still honoured for older callers; it is now the
            # default rather than the opt-in.
            argv += ["--triage"]
        return argv
    if action == "promote":
        return ["promote", tid()]
    if action == "specify":
        # Not inert: flips triage->todo via an LLM call, and the dispatcher
        # promotes todo->ready immediately afterward (no separate gate) —
        # see the UI-side comment on this same action for the incident that
        # established this. The confirm() dialog lives client-side; this
        # allowlist entry is what makes the action reachable at all.
        return ["specify", tid()]
    if action == "claim":
        return ["claim", tid()]
    if action == "request_review":
        return ["request-review", tid()]
    if action == "request_changes":
        reason = _short(body.get("reason"), 300)
        if not reason:
            raise ValueError("reason required")
        return ["request-changes", tid(), reason]
    if action == "complete":
        argv = ["complete", tid()]
        summary = _short(body.get("summary"), 300)
        if summary:
            argv += ["--summary", summary]
        return argv
    if action == "block":
        reason = _short(body.get("reason"), 300)
        if not reason:
            raise ValueError("reason required")
        kind = str(body.get("kind") or "needs_input")
        if kind not in _BLOCK_KINDS:
            raise ValueError("bad block kind")
        return ["block", tid(), reason, "--kind", kind]
    if action == "unblock":
        return ["unblock", tid()]
    if action == "reassign":
        profile = str(body.get("profile") or "")
        if profile != "none" and not _NAME_RE.match(profile):
            raise ValueError("bad profile")
        return ["reassign", tid(), profile]
    if action == "comment":
        text = _short(body.get("text"), 500)
        if not text:
            raise ValueError("text required")
        return ["comment", tid(), text, "--author", "console"]
    raise ValueError("unknown action")


def run_kanban(argv):
    try:
        r = subprocess.run(
            ["hermes", "kanban"] + argv, capture_output=True, text=True,
            timeout=60, env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
        msg = (r.stdout.strip() or r.stderr.strip() or "")[-500:]
        return r.returncode == 0, msg
    except Exception as exc:
        return False, str(exc)


# ── review gate + thesis conformance (P1) ─────────────────────────────────
# goldthread blocks edits to the thesis and re-injects it into worker
# context, but never verifies that what a worker PRODUCED conforms — the
# enforcement was write-only. This closes the loop: file a gt-review task
# whose acceptance criteria ARE the thesis Core Rules, one PASS/FAIL line
# each, and LINK it to the deliverable so the console can show which done
# tickets have actually been checked. A review created this way is
# thesis-aware by construction, not by whoever remembered to mention it.

_REVIEW_STAMP = re.compile(r"^t_[0-9a-f]{4,32}$")


def _task_row(task_id, cols):
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        row = con.execute(f"SELECT {cols} FROM tasks WHERE id = ?", (task_id,)).fetchone()
        con.close()
        return dict(row) if row else None
    except Exception:
        return None


def send_to_review(task_id):
    """Create a thesis-aware gt-review task for a deliverable and link it.
    Returns (ok, message). Two CLI calls (create + link), so it can't ride
    the single-argv kanban_argv path."""
    if not _REVIEW_STAMP.match(str(task_id or "")):
        return False, "bad task id"
    t = _task_row(task_id, "title, project_id, branch_name, status")
    if not t:
        return False, "task not found"

    # Resolve the project slug + thesis path for this deliverable.
    slug, thesis_path = None, None
    if t.get("project_id") and PROJECTS_DB.exists():
        try:
            con = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True, timeout=3)
            row = con.execute("SELECT slug, primary_path FROM projects WHERE id = ?",
                              (t["project_id"],)).fetchone()
            con.close()
            if row:
                slug = row[0]
                thesis_path = Path(row[1]) / "GOLD_THESIS.md"
        except Exception:
            pass

    rules = read_thesis(thesis_path)["rules"] if thesis_path else []
    criteria = "\n".join(f"  - [ ] Rule {r['n']} ({r['title']}): PASS / FAIL — cite the "
                         f"file+line that satisfies or violates it."
                         for r in rules) or "  - (no Core Rules parsed from the thesis)"
    branch = t.get("branch_name") or "(scratch task — no branch)"
    body = (
        f"THESIS-CONFORMANCE REVIEW of {task_id}: \"{_short(t['title'], 120)}\".\n\n"
        f"Deliverable branch: {branch}\n\n"
        f"This is a review task: do not modify the deliverable. Produce "
        f"docs/review/{task_id}-thesis-review.md with a verdict on EACH gold-"
        f"thesis rule, plus any correctness/security/accessibility findings.\n\n"
        f"Thesis rules to check (a FAIL on any of these is a [blocker]):\n{criteria}\n\n"
        f"End with an overall PASS/FAIL and the single most important fix.")

    argv = ["create", f"[thesis-review] {_short(t['title'], 100)}",
            "--body", body, "--assignee", "gt-review", "--created-by", "console",
            "--triage"]  # safe-default: parks for deliberate dispatch
    if slug:
        argv += ["--project", slug]
    ok, msg = run_kanban(argv)
    if not ok:
        return False, f"could not create review task: {msg}"
    # Pull the new task id out of "Created t_xxxx ..." and link it.
    m = re.search(r"\b(t_[0-9a-f]{4,32})\b", msg)
    if m:
        run_kanban(["link", task_id, m.group(1)])  # parent=deliverable, child=review
        return True, f"filed thesis review {m.group(1)} (in Backlog — dispatch it when ready)"
    return True, msg  # created but couldn't parse id to link


# ── release-level conformance (fix: per-task review has a real blind spot) ─
# Found reviewing PourRate's age gate: gt-review correctly passed Rule 3
# ("report and block ship in the same release as posting") for THAT diff,
# because the age gate alone introduces no posting surface — then had to
# hand-flag, as prose in its own findings, that Rule 3 actually needs the
# composer (already merged, lets you post) and report/block (still
# undispatched) TOGETHER, and nothing checks that combination on its own.
# Per-task review can only ever answer "does this diff violate a rule in
# isolation" — some rules are only satisfiable by several deliverables
# existing at once, and there was no check operating at that scope.
#
# This closes it the same way send_to_review closes the write-only-
# enforcement gap: file a real gt-review task, but scoped to the PROJECT'S
# CURRENT STATE (its default worktree checkout — i.e. everything merged so
# far) rather than one branch's diff. "Does this rule need something that
# doesn't exist yet" is a question about the whole codebase, not a diff.

def file_release_conformance_review(view_key, extra_projects):
    """File a release-level (not per-diff) thesis conformance review for a
    project. Returns (ok, message)."""
    views = load_views(extra_projects)
    view = next((v for v in views if v["key"] == view_key), None)
    if not view or view.get("kind") != "hermes":
        return False, "release conformance needs a real Hermes project view (not All work / Unfiled / an ad-hoc dir)"
    primary = view.get("path")
    if not primary or not os.path.isdir(primary):
        return False, "project has no resolvable primary folder"
    thesis = read_thesis(Path(primary) / "GOLD_THESIS.md")
    if not thesis["exists"]:
        return False, "no GOLD_THESIS.md for this project — nothing to check conformance against"
    rules = thesis["rules"]
    rules_block = "\n".join(f"  {r['n']}. {r['title']}" for r in rules) or "  (no Core Rules parsed from the thesis)"
    body = (
        "RELEASE-LEVEL THESIS CONFORMANCE REVIEW — this is explicitly NOT a "
        "review of one diff or one task's branch.\n\n"
        "This worktree is checked out from the project's current default "
        "branch: everything merged so far, i.e. what would actually ship if "
        "released today. Read the codebase as a WHOLE and assess whether it "
        "complies with GOLD_THESIS.md, rule by rule — not whether any single "
        "piece you can find violates a rule, but whether everything a rule "
        "requires is actually present TOGETHER right now.\n\n"
        "Several rules are only satisfiable by more than one feature existing "
        "at once (e.g. a rule requiring X to ship alongside Y, or a safety "
        "constraint that has to apply uniformly across every surface a user "
        "can reach — check this explicitly if the project has more than one "
        "distinct codebase/stack inside it, since a constraint implemented in "
        "one surface does NOT automatically apply to a completely separate "
        "one). A rule can look satisfied by a shallow grep and still be "
        "structurally missing for half the product — read enough to tell the "
        "difference.\n\n"
        f"Core Rules:\n{rules_block}\n\n"
        "Do not modify anything — this is a review. Produce "
        "docs/review/release-conformance.md with a verdict (PASS / FAIL / "
        "INCOMPLETE) per rule, citing the specific file(s) or feature(s) that "
        "satisfy it, or explicitly naming what's structurally missing and "
        "which rule that leaves unsatisfied. End with an overall verdict and "
        "the single most important gap, if any.")
    argv = ["create", f"[release-check] {view['name']} — thesis conformance",
            "--body", body, "--assignee", "gt-review", "--project", view["key"],
            "--created-by", "console", "--triage"]
    ok, msg = run_kanban(argv)
    if not ok:
        return False, f"could not create release conformance task: {msg}"
    m = re.search(r"\b(t_[0-9a-f]{4,32})\b", msg)
    if m:
        return True, f"filed release conformance review {m.group(1)} (in Backlog — dispatch it when ready)"
    return True, msg


def project_cost(view_key, extra_projects):
    """Sum real run cost across every task in a project (P3 rollup). Lazy —
    shells out to `sessions export` per run — so it lives on its own
    endpoint, never in the /api/state poll."""
    views = load_views(extra_projects)
    view = next((v for v in views if v["key"] == view_key), None)
    tasks, _, _ = read_tasks()
    if view and view.get("kind") == "hermes":
        scoped = [t for t in tasks if t.get("project_id") == view.get("project_id")]
    elif view and view.get("kind") == "unfiled":
        scoped = [t for t in tasks if not t.get("project_id")]
    else:
        scoped = tasks
    total, runs_counted, tasks_with_cost, pending, unpriced_runs = 0.0, 0, 0, 0, 0
    for t in scoped:
        art = read_artifact(t["id"])
        task_total = 0.0
        task_had_cost_data = False   # NOT `if task_total:` — a real $0.00
                                      # (fully cached run) is falsy in Python
                                      # and was silently excluded, undercounting
                                      # tasks_with_cost even though the sum was
                                      # still right. Found via a real Fable run.
        for run in art.get("runs", []):
            c = run.get("cost")
            if c and isinstance(c.get("cost_usd"), (int, float)):
                if c.get("cost_priced", True):
                    task_total += c["cost_usd"]
                else:
                    unpriced_runs += 1   # e.g. claude-fable-5: no Hermes price-table
                                          # entry yet — a real $0.00 would understate
                                          # spend, so it's excluded from the total
                                          # rather than silently counted as free.
                task_had_cost_data = True
                runs_counted += 1
                if c.get("cost_is_estimate"):
                    pending += 1
        if task_had_cost_data:
            tasks_with_cost += 1
            total += task_total
    return {"view": view_key, "name": view["name"] if view else view_key,
            "total_usd": round(total, 2), "runs_counted": runs_counted,
            "tasks_with_cost": tasks_with_cost, "tasks_scoped": len(scoped),
            "has_estimates": pending > 0, "unpriced_runs": unpriced_runs}


# ── local-first dispatch: cloud only escalates a task the local model
# actually failed ──────────────────────────────────────────────────────
# gt-research/infra/bakeoff/review went back to local models on 2026-08-24
# (see _LOCAL_OVERRIDE_PROFILES above) — local is the default again, the
# way the bake-off project was built for in the first place; cloud stopped
# being a parallel default and became a one-click recovery path for a task
# a local model has already, verifiably, failed at. "Failed" here means the
# same thing the P2 crash-reason fix already reads: status=blocked with a
# real FAILURE-outcome run behind it (crashed/timed_out/gave_up/
# spawn_failed/failed) — not merely blocked, and not a task still running.
# `hermes kanban set-model` pins the NEXT dispatch to a cloud model;
# `unblock` returns the task to ready so the gateway redispatches under
# that pin. The task itself, its id, and its history are untouched — this
# is a redispatch, not a new task.
_ESCALATE_STAMP = re.compile(r"^t_[0-9a-f]{4,32}$")


def escalate_task(task_id, model):
    if not _ESCALATE_STAMP.match(str(task_id or "")):
        return False, "bad task id"
    known = set(list_router_models()["models"])
    if model not in known:
        return False, f"unknown model: {model}"
    t = _task_row(task_id, "status, assignee")
    if not t:
        return False, "task not found"
    if t.get("status") != "blocked":
        return False, f"only a blocked task can be escalated (this one is {t.get('status')})"
    ok, msg = run_kanban(["set-model", task_id, model, "--provider", "anthropic"])
    if not ok:
        return False, f"could not pin model: {msg}"
    ok, msg = run_kanban(["unblock", task_id, "--reason",
                          f"escalated to {model} (cloud) after a local-model failure"])
    if not ok:
        return False, f"model pinned to {model}, but could not unblock: {msg}"
    return True, f"escalated to {model} — back in the queue for redispatch"


# ── model router: a big model plans, a cheaper model executes ─────────────
# The pattern the user wanted: hand a goal to a strong model (Fable/Opus),
# have it DECOMPOSE into concrete subtasks, and file each subtask pinned to
# a cheaper model (Sonnet/Haiku) that actually does the work. Console-
# orchestrated on purpose: the console runs the one planner call and files
# the children, so the executor model is GUARANTEED (not left to whether the
# planner remembered to pass --model) and every step is observable.
#
# "Under goldthread" is not decoration here: the planner call runs as
# `hermes -p <spoke> ...`, and every spoke now has goldthread enabled (the
# v0.3.2 coverage fix), so the pre_llm_call hook injects the thesis INTO the
# planner's context — it plans against the law — and the pre_tool_call guard
# is live if the planner reaches for a tool. The children dispatch to the
# same goldthread-covered spokes. Guard + injection + ledger cover the whole
# chain.

_ANTHROPIC_CACHE = HERMES_HOME / "provider_models_cache.json"
_MODEL_ID_RE = re.compile(r"^claude-[\w.-]{2,60}$")


def list_router_models():
    """The anthropic provider's real model ids, from Hermes' own cache — not
    a hardcoded list that rots. Split into a suggested planner/executor
    ordering purely for the UI's defaults; any model can be either."""
    ids = []
    try:
        d = json.loads(_ANTHROPIC_CACHE.read_text(encoding="utf-8"))
        ids = [m for m in d.get("anthropic", {}).get("models", [])
               if isinstance(m, str) and _MODEL_ID_RE.match(m)]
    except Exception:
        ids = ["claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"]
    # Rank big→small so the UI can default planner=biggest, executor=sonnet.
    def rank(m):
        for i, key in enumerate(("fable", "opus-5", "opus-4-8", "opus", "sonnet-5", "sonnet", "haiku")):
            if key in m:
                return i
        return 99
    ids = sorted(set(ids), key=rank)
    default_planner = next((m for m in ids if "fable" in m or "opus" in m), ids[0] if ids else None)
    default_executor = next((m for m in ids if "sonnet" in m), ids[-1] if ids else None)
    # These models run on the anthropic provider, so the planner call and the
    # child tasks only work on a profile that can actually take a
    # --provider anthropic override right now — checked live via
    # _anthropic_capable, not read off the profile's default provider (see
    # that function's comment for why the old check would have gone empty
    # the moment the local rebuild below landed). gt-pm is excluded as the
    # PM hub, not a work-executing spoke; gt-dumbq is excluded because it's
    # deliberately the weak/dumb-question spoke — neither exclusion is about
    # model capability.
    eligible = [n for n, _, kind in SPOKES
                if kind == "spoke" and n != "gt-dumbq" and _anthropic_capable(n)]
    return {"models": ids, "default_planner": default_planner,
            "default_executor": default_executor, "assignees": eligible}


def _anthropic_capable(profile):
    """Whether `profile` can take a --provider anthropic override right now
    — checked live against its own credential pool, not assumed from its
    configured default model. Verified live 2026-08-24: gt-pm, whose
    default provider is ollama-local, still answered a real
    --provider anthropic call correctly — Hermes auth turned out to be
    independent of a profile's default model, so the earlier "refused for
    ollama-only profiles" finding was really about missing credentials on
    THOSE specific profiles, not about local-primary profiles in general."""
    try:
        r = subprocess.run(["hermes", "-p", profile, "auth", "status", "anthropic"],
                           capture_output=True, text=True, timeout=6,
                           env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
        return "logged in" in (r.stdout or "").lower()
    except Exception:
        return False


_ANTHROPIC_PROFILE = _anthropic_capable


_PLAN_PROMPT = (
    "You are a planning model. Break the GOAL below into {lo}-{hi} concrete, "
    "independently-executable engineering subtasks. Each subtask must have:\n"
    "- `title`: short imperative\n"
    "- `body`: what to build or do, plus acceptance criteria a worker can check\n"
    "- `difficulty`: one of \"low\", \"medium\", \"high\" — your honest estimate of "
    "how much model capability it needs (low = mechanical/boilerplate, high = "
    "subtle design, tricky correctness, or security-sensitive). This drives which "
    "model executes it, so rate it truthfully.\n"
    "The project's GOLD_THESIS.md is law — every subtask must comply with it; do "
    "not propose work that violates a thesis rule.{structure}\n\n"
    "Output ONLY a JSON array and "
    "nothing else — no prose, no markdown fence:\n"
    '[{{"title":"...","body":"...","difficulty":"medium"}}]\n\nGOAL:\n{goal}')

# Each subtask is executed by a SEPARATE worker in an ISOLATED worktree —
# they cannot see each other's code as it's being written, only what's
# already merged when the planner runs. Confirmed live on PourRate: an
# "age gate" subtask and a "rating composer" subtask, with no shared
# context, came back as a Swift package and a completely disconnected
# React+Python app — not two parts of one product, two products sharing a
# repo. The release-conformance check then found the real consequence:
# the age gate covers the Swift surface only, and the ONLY surface that
# can actually post content has no gate at all, because nothing connects
# the two codebases. This can't fully prevent that — workers in the SAME
# route call still can't see each other's in-flight work — but it closes
# the cheaper, avoidable half: a SECOND route call against a project that
# already has real code should not blindly re-derive a different stack
# from scratch. Tell the planner what already exists.
def _existing_structure_note(primary):
    if not primary or not os.path.isdir(primary):
        return ""
    html = _scan_html_artifacts(primary)
    other = _scan_other_ui_source(primary)
    if not html and not other:
        return ""
    lines = []
    if html:
        by_dir = {}
        for h in html:
            by_dir.setdefault(h["dir"], []).append(h["file"])
        lines.append("Existing UI files: " + "; ".join(
            f"{d}/ ({', '.join(files)})" for d, files in by_dir.items()))
    if other:
        # Enough to name the stack(s) in play without dumping every file.
        # ", ".join, not "/".join — these are separate roots, not a nested
        # path, and "Sources/src" would misleadingly read as one.
        roots = sorted({f.split("/")[0] for f in other})
        exts = sorted({os.path.splitext(f)[1] for f in other})
        lines.append(f"Existing source under {', '.join(roots)}/ "
                     f"({', '.join(exts)} — {len(other)} file(s))")
    return (
        "\n\nThis project already has code in it — read the list below before "
        "choosing an approach. If a subtask's work naturally extends what's "
        "already there, say so explicitly in its body and keep it in the SAME "
        "stack/language rather than starting a new one; only introduce a new "
        "stack if the goal genuinely calls for a separate surface (e.g. a "
        "native app alongside a web app), and if you do, say explicitly in "
        "the body that this subtask's work needs to be wired into what "
        "already exists — a subtask built in isolation from an existing "
        "safety-critical surface (like an age gate) does not automatically "
        "protect that surface.\n" + "\n".join(f"- {l}" for l in lines))

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _parse_subtasks(text):
    """Pull a [{title,body},...] array out of a model's reply, tolerating
    markdown fences and surrounding prose. Returns (list, error)."""
    if not text:
        return [], "planner returned no output"
    # Strip ANSI: escape sequences contain '[', which the array regex would
    # otherwise latch onto before the real JSON.
    text = _ANSI_RE.sub("", text)
    m = _JSON_ARRAY_RE.search(text)
    if not m:
        return [], "planner output had no JSON array"
    try:
        arr = json.loads(m.group(0))
    except Exception as exc:
        return [], f"planner output was not valid JSON: {exc}"
    if not isinstance(arr, list):
        return [], "planner output was not a list"
    out = []
    for item in arr[:8]:  # hard cap
        if not isinstance(item, dict):
            continue
        title = _short(item.get("title"), 200)
        body = _short(item.get("body"), 4000)
        diff = str(item.get("difficulty") or "medium").strip().lower()
        if diff not in ("low", "medium", "high"):
            diff = "medium"
        if title:
            out.append({"title": title, "body": body, "difficulty": diff})
    if not out:
        return [], "planner produced no usable subtasks"
    return out, None


def run_route(body):
    """Plan with a big model, file subtasks pinned to a cheaper one.
    Returns (ok, result_dict_or_message)."""
    goal = _short(body.get("goal"), 4000)
    if not goal:
        return False, "goal required"
    planner = str(body.get("planner") or "")
    executor = str(body.get("executor") or "")
    known = set(list_router_models()["models"])
    if planner not in known:
        return False, f"unknown planner model: {planner}"
    if executor not in known:
        return False, f"unknown executor model: {executor}"
    assignee = str(body.get("assignee") or "")
    if assignee and assignee not in {n for n, _, _ in SPOKES}:
        return False, "unknown assignee"
    # The planner and executor are anthropic models, so the assignee profile
    # needs a working --provider anthropic override, checked live (see
    # _anthropic_capable) rather than assumed from its default provider —
    # refuse here with a clear message rather than filing tasks pinned to a
    # model the profile can't actually reach.
    router_eligible = list_router_models()["assignees"]  # already in SPOKES order
    plan_profile = assignee or (router_eligible[0] if router_eligible else "gt-research")
    if plan_profile not in router_eligible:
        return False, (f"{plan_profile} isn't a router-eligible spoke right now — pick one of "
                       f"{', '.join(sorted(router_eligible)) or '(none currently eligible)'}.")
    project = str(body.get("project") or "")
    if project and not _NAME_RE.match(project):
        return False, "bad project"
    try:
        lo, hi = int(body.get("min") or 2), int(body.get("max") or 6)
    except Exception:
        lo, hi = 2, 6
    lo, hi = max(1, min(lo, 8)), max(1, min(hi, 8))

    # Cost-aware per-subtask routing (the LLMRouter idea, applied to the
    # decomposition): instead of pinning every child to one model, map the
    # planner's own difficulty rating to a model tier — low→cheapest,
    # medium→the chosen executor, high→the planner model. Off = every child
    # gets the executor, flat.
    cost_aware = bool(body.get("cost_aware", True))
    ladder = list_router_models()["models"]
    cheapest = next((m for m in reversed(ladder) if "haiku" in m), None) \
        or (ladder[-1] if ladder else executor)

    def model_for(difficulty):
        if not cost_aware:
            return executor
        return {"low": cheapest, "medium": executor, "high": planner}.get(difficulty, executor)

    # 1) Parent umbrella task (the goal). Stays in triage forever — it's a
    #    readable record of the goal, not meant to ever be dispatched or
    #    completed. This means it must NEVER become a Hermes dependency
    #    parent: `hermes kanban link`/`create --parent` both write to the
    #    same task_links table promote() reads to enforce "child cannot
    #    promote until parent completes" (confirmed live — every subtask
    #    linked this way got stuck at 'todo' forever with "unsatisfied
    #    parent dependencies", since an umbrella task that's never claimed
    #    can never legitimately reach 'done'). So the umbrella's id is
    #    referenced in each child's body for a human/model to trace back to
    #    the original goal, and NOTHING calls task_links for it.
    pargv = ["create", f"[plan] {goal.splitlines()[0][:100]}",
             "--body", f"Umbrella goal, decomposed by {planner}. This task is "
                       f"informational only — it deliberately stays in triage "
                       f"forever and is never linked as a dependency, because "
                       f"Hermes blocks a child's promotion until its parent "
                       f"completes and this task is never meant to be worked. "
                       f"The subtasks below reference this id for traceability.\n\n"
                       f"GOAL:\n{goal}",
             "--created-by", "router", "--triage"]
    if assignee:
        pargv += ["--assignee", assignee]
    if project:
        pargv += ["--project", project]
    ok, msg = run_kanban(pargv)
    if not ok:
        return False, f"could not create the goal task: {msg}"
    pm = re.search(r"\b(t_[0-9a-f]{4,32})\b", msg)
    parent_id = pm.group(1) if pm else None

    # 2) The planner call — a REAL big-model run, in a goldthread-covered
    #    spoke so the thesis is injected into its context. (plan_profile was
    #    resolved and anthropic-checked up in the validation block.)
    cwd = None
    if project and PROJECTS_DB.exists():
        try:
            con = sqlite3.connect(f"file:{PROJECTS_DB}?mode=ro", uri=True, timeout=3)
            row = con.execute("SELECT primary_path FROM projects WHERE slug = ?",
                              (project,)).fetchone()
            con.close()
            if row and os.path.isdir(row[0]):
                cwd = row[0]  # run planner in the project dir so it can read the thesis
        except Exception:
            pass
    prompt = _PLAN_PROMPT.format(goal=goal, lo=lo, hi=hi, structure=_existing_structure_note(cwd))
    try:
        r = subprocess.run(
            # -Q = programmatic quiet: only the model's final response on
            # stdout (banner/spinner/tool previews suppressed). stderr, which
            # carries deprecation noise, is captured separately and ignored.
            ["hermes", "-p", plan_profile, "-m", planner, "--provider", "anthropic",
             "chat", "-Q", "-q", prompt],
            capture_output=True, text=True, timeout=180, cwd=cwd,
            env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
        planner_out = r.stdout or ""
    except subprocess.TimeoutExpired:
        return False, (f"planner ({planner}) timed out after 180s. The goal task "
                       f"{parent_id or ''} was created; retry the plan or decompose by hand.")
    except Exception as exc:
        return False, f"planner call failed: {exc}"

    subtasks, perr = _parse_subtasks(planner_out)
    if perr:
        return False, (f"{perr}. The goal task {parent_id or ''} exists; you can "
                       f"re-run the router or file subtasks by hand.")

    # 3) File each subtask pinned to its routed model. Deliberately NOT
    #    --parent'd to the umbrella task (see the comment above parent_id's
    #    creation) — the umbrella id goes in the body text only, so a human
    #    or worker can trace it without Hermes treating it as a dependency
    #    that can never be satisfied.
    filed = []
    for st in subtasks:
        child_model = model_for(st["difficulty"])
        child_body = st["body"]
        if parent_id:
            child_body += f"\n\n(Part of the plan filed as {parent_id}.)"
        cargv = ["create", st["title"], "--body", child_body,
                 "--model", child_model, "--provider", "anthropic",
                 "--created-by", "router", "--triage"]
        if assignee:
            cargv += ["--assignee", assignee]
        if project:
            cargv += ["--project", project]
        cok, cmsg = run_kanban(cargv)
        cm = re.search(r"\b(t_[0-9a-f]{4,32})\b", cmsg)
        filed.append({"title": st["title"], "id": cm.group(1) if cm else None,
                      "difficulty": st["difficulty"], "model": child_model,
                      "ok": cok, "error": None if cok else cmsg[:160]})

    ok_ct = sum(1 for f in filed if f["ok"])
    return True, {
        "parent_id": parent_id, "planner": planner, "executor": executor,
        "cost_aware": cost_aware, "assignee": assignee or "(unassigned)", "subtasks": filed,
        "filed_ok": ok_ct, "filed_total": len(filed),
        "message": (f"{planner} planned {len(filed)} subtask(s); filed {ok_ct} "
                    + ("cost-routed by difficulty" if cost_aware
                       else f"pinned to {executor}")
                    + ", in Backlog. Dispatch when ready."),
    }


def host_ok(headers):
    """DNS-rebinding defense for the READ endpoints. origin_ok() already
    stops rebound pages from POSTing (a rebound page's fetch still sends
    its real Origin), but GET has no such tell: a hostile page whose domain
    re-resolves to 127.0.0.1 becomes same-origin with this server and can
    read /api/state — the whole board plus the full thesis text. The one
    thing rebinding cannot forge is the Host header, which stays the
    attacker's domain. Legitimate access is by IP or localhost only."""
    host = (headers.get("Host") or "").strip()
    return bool(re.match(r"^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", host))


def origin_ok(headers):
    """Localhost-CSRF defense. The bind is 127.0.0.1, but a hostile web page
    in the user's own browser can still fire POSTs at localhost ports. The
    custom X-GT-Console header forces a CORS preflight for any cross-origin
    caller — and since we never answer preflights, such requests die before
    they are ever sent. The Origin check is belt to that suspenders."""
    origin = headers.get("Origin")
    if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
        return False
    return headers.get("X-GT-Console") == "1"


# ── http ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    extra_projects = []

    def _reject_human_only(self, body):
        """Raise HumanOnlyError if the action's target task is human-only.

        Looks up title/tenant FRESH from kanban.db — never trusts anything
        the client sent about the task's own marker status, so a stale or
        tampered client payload can't talk its way past this. `comment` is
        exempt (leaving a note is how a human explains what they did; it
        can't dispatch anything). `create` is exempt because it has no
        target task yet — filing something new is handled separately by
        the client defaulting new tasks to triage, not by this check.
        """
        action = body.get("action")
        task_id = body.get("task_id")
        if action in ("create", "comment") or not task_id or not _ID_RE.match(str(task_id)):
            return
        try:
            con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=3)
            row = con.execute("SELECT title, tenant FROM tasks WHERE id = ?",
                              (task_id,)).fetchone()
            con.close()
        except Exception:
            return  # DB unreadable — kanban_argv/hermes CLI will surface the real error
        if row and is_human_only({"title": row[0], "tenant": row[1]}):
            raise HumanOnlyError(
                f"{task_id} is marked human-only — resolve it by hand at the "
                f"keyboard, not through the console or any kanban verb.")

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not host_ok(self.headers):
            return self._send(403, "forbidden", "text/plain")
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            f = HERE / "index.html"
            if not f.exists():
                return self._send(500, "index.html missing", "text/plain")
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            q = parse_qs(u.query)
            view = (q.get("view") or ["all"])[0]
            try:
                payload = json.dumps(
                    build_state(view, self.extra_projects), default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        if u.path == "/api/design":
            q = parse_qs(u.query)
            view = (q.get("view") or ["all"])[0]
            try:
                payload = json.dumps(
                    read_design(view, self.extra_projects), default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        if u.path == "/api/artifact":
            q = parse_qs(u.query)
            task_id = (q.get("task_id") or [""])[0]
            try:
                payload = json.dumps(read_artifact(task_id), default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        if u.path == "/api/health":
            q = parse_qs(u.query)
            profile = (q.get("profile") or [""])[0]
            if not _NAME_RE.match(profile) or profile not in {n for n, _, _ in SPOKES}:
                return self._send(400, json.dumps({"error": "unknown profile"}),
                                  "application/json")
            try:
                payload = json.dumps(check_health(profile), default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        if u.path == "/api/models":
            try:
                local = list_local_models()
                payload = json.dumps({
                    "local": local,
                    "local_hints": {m: _local_model_hint(m) for m in local},
                    "local_provider": _LOCAL_OVERRIDE_PROVIDER,
                    "local_override_profiles": sorted(_LOCAL_OVERRIDE_PROFILES),
                }, default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        if u.path == "/api/router-models":
            try:
                payload = json.dumps(list_router_models(), default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        if u.path == "/api/cost":
            q = parse_qs(u.query)
            view = (q.get("view") or ["all"])[0]
            try:
                payload = json.dumps(project_cost(view, self.extra_projects), default=str)
            except Exception as exc:
                return self._send(500, json.dumps({"error": str(exc)}),
                                  "application/json")
            return self._send(200, payload, "application/json")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/kanban":
            return self._send(404, "not found", "text/plain")
        if not host_ok(self.headers) or not origin_ok(self.headers):
            return self._send(403, json.dumps({"ok": False, "message": "forbidden"}),
                              "application/json")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, json.dumps({"ok": False, "message": "json only"}),
                              "application/json")
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
            body = json.loads(self.rfile.read(length) or b"{}")
            # send_to_review is multi-step (create + link) and thesis-aware,
            # so it doesn't fit the single-argv allowlist — handle it here.
            if body.get("action") == "send_to_review":
                ok, msg = send_to_review(body.get("task_id"))
                return self._send(200 if ok else 502,
                                  json.dumps({"ok": ok, "message": msg}), "application/json")
            if body.get("action") == "release_conformance":
                ok, msg = file_release_conformance_review(body.get("view"), self.extra_projects)
                return self._send(200 if ok else 502,
                                  json.dumps({"ok": ok, "message": msg}), "application/json")
            if body.get("action") == "escalate":
                ok, msg = escalate_task(body.get("task_id"), body.get("model"))
                return self._send(200 if ok else 502,
                                  json.dumps({"ok": ok, "message": msg}), "application/json")
            # The router runs a real planner call (up to ~3 min) and files
            # subtasks — multi-step, so it's handled here, not via kanban_argv.
            if body.get("action") == "route":
                ok, res = run_route(body)
                out = {"ok": ok}
                if isinstance(res, dict):
                    out.update(res)
                else:
                    out["message"] = res
                return self._send(200 if ok else 502, json.dumps(out, default=str),
                                  "application/json")
            argv = kanban_argv(body)
            self._reject_human_only(body)
        except HumanOnlyError as exc:
            return self._send(403, json.dumps({"ok": False, "message": str(exc)}),
                              "application/json")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, json.dumps({"ok": False, "message": str(exc)}),
                              "application/json")
        ok, msg = run_kanban(argv)
        self._send(200 if ok else 502,
                   json.dumps({"ok": ok, "message": msg}), "application/json")

    def log_message(self, fmt, *args):
        pass  # the console polls; don't spam the terminal


def parse_project_arg(s):
    if "=" not in s:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    name, path = s.split("=", 1)
    return (name.strip(), str(Path(path).expanduser().resolve()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9120)
    ap.add_argument("--project", action="append", type=parse_project_arg,
                    default=[], metavar="NAME=PATH",
                    help="ad-hoc thesis dir shown as a sidebar view "
                         "(Hermes projects appear automatically)")
    a = ap.parse_args()
    Handler.extra_projects = a.project

    st = build_state("all", a.project)
    print(f"goldthread console  →  http://127.0.0.1:{a.port}", flush=True)
    print(f"  hermes home : {HERMES_HOME}")
    names = [v["name"] for v in st["views"]]
    print(f"  views       : {', '.join(names)}", flush=True)
    print(f"  board       : {st['board']['board_total']} task(s)"
          f"{'  — ' + st['board']['error'] if st['board']['error'] else ''}")
    print(f"  ledger      : {'present' if st['ledger']['exists'] else 'not created yet'}", flush=True)
    gw = st["health"]["gateway_running"]
    print(f"  gateway     : {'running' if gw else 'not running (ready tasks will not dispatch)'}", flush=True)
    kc = st["health"]["kanban_config"]
    if gw and kc["auto_decompose"]:
        print(f"  automation  : auto_decompose ON — filed tasks are groomed and "
              f"promoted to ready, then dispatched (~{kc['dispatch_interval_seconds']}s tick).")
        print("                Backlog does NOT hold work. To park it: "
              "hermes config set kanban.auto_decompose false")
    print("  board edits go through `hermes kanban`; kanban.db is never "
          "written directly\n", flush=True)

    # 127.0.0.1, not 0.0.0.0: this exposes board contents and the thesis, and
    # binding it to every interface is a network-exposure decision the user
    # should make deliberately, not a default.
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
