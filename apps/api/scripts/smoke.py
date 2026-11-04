"""make smoke — hit /health on the local API, assert status is non-down."""

from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    url = "http://localhost:8000/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — fixed localhost URL
            if resp.status != 200:
                print(f"smoke fail: HTTP {resp.status}", file=sys.stderr)
                return 1
            body = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(f"smoke fail: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(json.dumps(body, indent=2))
    if body.get("status") == "down":
        print("smoke fail: status=down", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
