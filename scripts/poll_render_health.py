"""Poll Render /health and /readiness until stable."""
from __future__ import annotations

import json
import time
import urllib.request

URL = "https://example-backend.onrender.com"


def get(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{URL}{path}", timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return 0, {"error": str(exc)}


def main() -> None:
    for i in range(40):
        h_code, h = get("/health")
        r_code, r = get("/readiness")
        print(f"[{i+1}] health={h_code} {h} readiness={r_code} {r}")
        if h_code == 200 and r_code == 200:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
