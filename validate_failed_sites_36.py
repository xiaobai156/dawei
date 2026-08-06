#!/usr/bin/env python3
"""Compatibility entry for the V2 failed-site validation CLI."""

from dawei.cli.validate_failed import main


if __name__ == "__main__":
    raise SystemExit(main())
