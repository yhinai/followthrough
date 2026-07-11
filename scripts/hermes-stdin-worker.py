#!/usr/bin/env python3
"""Run Hermes oneshot with a prompt from stdin so private text never appears in argv."""

from __future__ import annotations

import sys

from hermes_cli.oneshot import run_oneshot


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    prompt = sys.stdin.read()
    if not prompt:
        return 2
    # Planning is deliberately tool-isolated. `vision` is read-only and keeps
    # the configured terminal, filesystem, browser, memory and messaging tools
    # out of reach of untrusted transcript content.
    return run_oneshot(prompt, usage_file=sys.argv[1], toolsets="vision")


if __name__ == "__main__":
    raise SystemExit(main())
