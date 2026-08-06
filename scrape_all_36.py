#!/usr/bin/env python3
"""Compatibility entry for the V2 single-issue CLI."""

from dawei.cli.single_issue import main


if __name__ == "__main__":
    raise SystemExit(main())
