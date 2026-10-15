#!/usr/bin/env python3
"""Lightweight CI scan for common accidentally committed credentials.

This is a guardrail, not a replacement for a dedicated secret-scanning service.
It reports filenames and pattern labels only, never matched values.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    ("GitHub classic token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile("github" + r"_pat_[A-Za-z0-9_]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
)
IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules"}


def scan_text(text: str) -> list[str]:
    return [label for label, pattern in PATTERNS if pattern.search(text)]


def main() -> int:
    findings: list[tuple[str, str]] = []
    for path in ROOT.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"Could not read {path.relative_to(ROOT)}: {exc}", file=sys.stderr)
            return 2
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for label in scan_text(text):
            findings.append((path.relative_to(ROOT).as_posix(), label))

    if findings:
        print("Potential committed secret pattern(s) found:")
        for path, label in findings:
            print(f"  {path}: {label}")
        return 1
    print("No common committed secret patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
