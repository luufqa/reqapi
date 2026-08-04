#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="REQAPI HTTP healthcheck")
    parser.add_argument("--url", default="http://127.0.0.1:8765/api/me")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    request = urllib.request.Request(args.url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            return 0 if 200 <= response.status < 500 else 1
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
