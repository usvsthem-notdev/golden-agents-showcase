#!/usr/bin/env python3
"""Red-team the goldthread guard. Calls pre_tool_call directly and reports
which mutation attempts it ALLOWS. No filesystem writes happen here.

PUBLIC-COPY NOTE: the exact evasion payloads (glob patterns, quote-split
syntax, backslash escaping, variable indirection, printf tricks, etc.)
that found six real holes in an earlier version of this guard have been
redacted from this shared copy. The current guard blocks all of them, but
the techniques are generically useful against any similarly naive file
guard, not just this one, so the working commands aren't published here.
See security/README.md for the categories found and fixed.
"""
import sys, os, subprocess, tempfile, pathlib
sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/goldthread"))
import guard

def allowed(tool, args):
    return guard.pre_tool_call(tool_name=tool, args=args) is None

SHELL = [
    ("plain redirect",   'echo pwned > GOLD_THESIS.md', True),
    ("sed -i",           'sed -i "" s/a/b/ GOLD_THESIS.md', True),
    ("truncate",         'truncate -s 0 GOLD_THESIS.md', True),
    ("find -delete",     'find . -name GOLD_THESIS.md -delete', True),
    ("rm",               'rm GOLD_THESIS.md', True),
    ("legit read cat",   'cat GOLD_THESIS.md', False),
    ("legit read grep",  'grep Rule GOLD_THESIS.md', False),
    ("<redacted: glob evasion>",         None, True),
    ("<redacted: quote-splitting>",      None, True),
    ("<redacted: case-folding>",         None, True),
    ("<redacted: variable indirection>", None, True),
    ("<redacted: shell-expansion trick>", None, True),
]

FILE_TOOL = [
    ("write exact",        {"file_path": "GOLD_THESIS.md"}, True),
    ("write ./",           {"file_path": "./GOLD_THESIS.md"}, True),
    ("write abs",          {"file_path": "/tmp/x/GOLD_THESIS.md"}, True),
    ("write dotdot",       {"file_path": "sub/../GOLD_THESIS.md"}, True),
    ("write other file",   {"file_path": "AMENDMENTS.md"}, False),
]

print("=" * 78)
print("SHELL COMMANDS  (tool_name='bash')")
print("=" * 78)
holes = []
for label, cmd, should_block in SHELL:
    if cmd is None:
        print(f"{'(redacted)':8}  {label:38}  payload withheld from public copy")
        continue
    a = allowed("bash", {"command": cmd})
    verdict = "ALLOWED" if a else "blocked"
    bad = (should_block and a) or (not should_block and not a)
    flag = "  <-- HOLE" if (should_block and a) else ("  <-- false positive" if bad else "")
    if bad:
        holes.append((label, cmd, "allowed a write" if should_block else "blocked a read"))
    print(f"{verdict:8}  {label:38}  {cmd[:44]:44}{flag}")

print()
print("=" * 78)
print("FILE TOOLS  (tool_name='write_file')")
print("=" * 78)
for label, args, should_block in FILE_TOOL:
    a = allowed("write_file", args)
    verdict = "ALLOWED" if a else "blocked"
    bad = (should_block and a) or (not should_block and not a)
    flag = "  <-- HOLE" if (should_block and a) else ("  <-- false positive" if bad else "")
    if bad:
        holes.append((label, str(args), "allowed a write" if should_block else "blocked a read"))
    print(f"{verdict:8}  {label:28}  {str(args)[:44]:44}{flag}")

print()
print("=" * 78)
print("FILESYSTEM REALITY CHECK")
print("=" * 78)
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "GOLD_THESIS.md"
    p.write_text("original\n")
    os.chmod(p, 0o444)
    lower = pathlib.Path(d) / "gold_thesis.md"
    case_insensitive = lower.exists()
    print(f"case-insensitive filesystem: {case_insensitive}"
          f"   ('gold_thesis.md' resolves to the same file: {case_insensitive})")
    r = subprocess.run(f'cd {d} && echo pwned > GOLD_THESIS.md',
                       shell=True, capture_output=True, text=True)
    print(f"redirect onto chmod-444 file: rc={r.returncode} "
          f"({'REFUSED by OS' if r.returncode else 'OVERWROTE'})")
    r2 = subprocess.run(f'cd {d} && rm -f GOLD_THESIS.md && echo gone',
                        shell=True, capture_output=True, text=True)
    print(f"rm -f on chmod-444 file:      rc={r2.returncode} "
          f"({'file DELETED — mode 444 does not protect' if r2.returncode == 0 else 'refused'})")

print()
print("=" * 78)
print(f"SUMMARY: {len(holes)} problem(s) (redacted rows not counted)")
for label, cmd, kind in holes:
    print(f"  - [{kind}] {label}: {cmd[:60]}")
