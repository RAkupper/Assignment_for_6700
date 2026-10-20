"""Check for Docker health"""

import json
import sys
from urllib.request import urlopen


def check_health(
    url: str = "http://127.0.0.1:8000/health",
    timeout: float = 3.0,
) -> bool:
    """Return whether the service reports a healthy status"""
    try:
        with urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            payload = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def main() -> int:
    """Return an exit Docker"""
    if check_health():
        return 0

    print("Service health check failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
