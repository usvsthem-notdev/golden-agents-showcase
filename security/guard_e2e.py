#!/usr/bin/env python3
"""End-to-end: for each bypass the guard ALLOWS, actually run it against a
real sealed (chmod 444) GOLD_THESIS.md in a throwaway dir and report whether
the thesis actually changed. Guard-allowed + file-changed = real breach.

PUBLIC-COPY NOTE: this harness's whole point is running working exploit
commands against a real sealed file, so its exploit list is redacted
entirely for this shared copy rather than partially neutered. See
security/README.md for what this found and how it was fixed; the runnable
version lives in the private working repo.
"""
import sys, os, subprocess, tempfile, pathlib, hashlib
sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/goldthread"))
import guard

ORIGINAL = "# Gold Thesis\n\n## Core Rules\n1. **Immutable to agents.**\n"
CANDIDATES = []

def digest(p):
    if not p.exists():
        return "<<DELETED>>"
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]

if not CANDIDATES:
    print("Exploit payload list redacted from this public copy.")
    print("See security/README.md for the categories this found and fixed.")
    sys.exit(0)
