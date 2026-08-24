#!/usr/bin/env python3
"""False-positive check: the hardened guard must still ALLOW legitimate work,
including in a directory that contains the thesis. An over-blocking guard is
a guard that gets turned off.

PUBLIC-COPY NOTE: three MUST_BLOCK entries that were specific evasion
payloads have been replaced below with plain, non-evasive mutation
attempts. See security/README.md for the categories those covered.
"""
import sys, os, tempfile, pathlib
sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/goldthread"))
import guard

# Commands run FROM a directory that contains GOLD_THESIS.md — the hostile
# case for false positives, since the thesis is in reach of every glob.
MUST_ALLOW = [
    'cat GOLD_THESIS.md',
    'grep -n "Rule" GOLD_THESIS.md',
    'head -20 GOLD_THESIS.md',
    'git show HEAD:GOLD_THESIS.md',
    'git log --oneline',
    'git diff',
    'git status',
    'echo $HOME',
    'ls -la',
    'cat README.md',
    'echo "building" > build.log',
    'rm -f build.log',
    'mv src/a.js src/b.js',
    'python3 -c "print(1)"',
    'sed -i "" s/a/b/ src/app.js',
    'npm install',
    'find . -name "*.js" -delete',
    'cp package.json package.json.bak',
    'echo $PATH > /tmp/path.txt',
    'chmod 644 notes.md',
    'tee AMENDMENTS.md',
    'cat design/cat-profile-form.html',
    'grep -r "thesis" docs/',          # talking ABOUT it is legal
    'echo "see GOLD_THESIS for rules" > notes.md',  # word without extension
]

MUST_BLOCK = [
    'echo x > GOLD_THESIS.md',
    'rm GOLD_THESIS.md',
    'rm -f GOLD_THESIS.md',
    'chmod 644 GOLD_THESIS.md',
    'sed -i "" s/a/b/ GOLD_THESIS.md',
    'mv GOLD_THESIS.md elsewhere.md',
    'find . -name "GOLD_THESIS.md" -delete',
]

fails = []
with tempfile.TemporaryDirectory() as d:
    dd = pathlib.Path(d)
    (dd / "GOLD_THESIS.md").write_text("# thesis\n")
    (dd / "README.md").write_text("readme\n")
    (dd / "notes.md").write_text("notes\n")
    (dd / "package.json").write_text("{}\n")
    (dd / "AMENDMENTS.md").write_text("\n")
    (dd / "src").mkdir(); (dd / "src" / "a.js").write_text("x\n")
    (dd / "docs").mkdir(); (dd / "docs" / "d.md").write_text("x\n")
    (dd / "design").mkdir(); (dd / "design" / "cat-profile-form.html").write_text("x\n")
    prev = os.getcwd(); os.chdir(d)
    try:
        print("MUST ALLOW (legitimate work, run next to the thesis)")
        print("-" * 70)
        for cmd in MUST_ALLOW:
            ok = guard.pre_tool_call(tool_name="bash", args={"command": cmd}) is None
            if not ok:
                fails.append(("FALSE POSITIVE", cmd))
            print(f"  {'allow ' if ok else 'BLOCK!':7} {cmd}")
        print()
        print("MUST BLOCK (real mutation attempts)")
        print("-" * 70)
        for cmd in MUST_BLOCK:
            ok = guard.pre_tool_call(tool_name="bash", args={"command": cmd}) is None
            if ok:
                fails.append(("MISSED BREACH", cmd))
            print(f"  {'ALLOW!' if ok else 'block ':7} {cmd}")
    finally:
        os.chdir(prev)

# file-tool paths
print()
print("FILE TOOLS")
print("-" * 70)
for args, should_block in [
    ({"file_path": "memory/AMENDMENTS.md"}, False),
    ({"file_path": "design/form.html"}, False),
    ({"file_path": "GOLD_THESIS.md"}, True),
    ({"file_path": "gold_thesis.md"}, True),
    ({"file_path": "GOLD_THESIS.md/"}, True),
    ({"file_path": "docs/GOLD_THESIS.md"}, True),
]:
    ok = guard.pre_tool_call(tool_name="write_file", args=args) is None
    bad = ok if should_block else (not ok)
    if bad:
        fails.append(("FALSE POSITIVE" if not should_block else "MISSED BREACH", str(args)))
    print(f"  {'allow ' if ok else 'block ':7} {args}  {'<-- WRONG' if bad else ''}")

print()
print("=" * 70)
if fails:
    print(f"FAILURES: {len(fails)}")
    for kind, c in fails:
        print(f"  [{kind}] {c}")
    sys.exit(1)
print("All clear: no false positives, no missed breaches.")
