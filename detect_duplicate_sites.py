#!/usr/bin/env python3
"""Compatibility entry for the V2 duplicate-detection CLI."""

from dawei.cli.duplicate import main


if __name__ == "__main__":
    raise SystemExit(main())
