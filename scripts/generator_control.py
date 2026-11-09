"""Control run: measures the load generator itself against the gateway's static /health
endpoint (no application work), to find the highest rate the harness can produce reliably."""
import asyncio
import json
import sys

import ftlab as L

rate = float(sys.argv[1])


def health(u):
    return "GET", "/health", {}, "health"


res = asyncio.run(L.run_load(duration=15, rate=lambda t: rate, users=[L.User("x", "t", 1)], mix=[(1, health)]))
s = L.summarize(res, None)
print(json.dumps({"rate": rate, "success_ratio": s["success_ratio"], "p95_ms": s["latency_p95_ms"],
                  "send_rate": s["send_rate_rps"]}))
