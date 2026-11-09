"""E5 load generator, executed inside the Docker network (see run_experiments.e5_load_spike).

The host -> Docker Desktop port-forwarding path on Windows saturates at roughly
150-200 requests/s, so the spike is generated from a container next to the gateway.
Prints the run as JSON on stdout.
"""
import asyncio
import json
import sys

import ftlab as L

mode, rep, base_rate, spike_rate = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
users = L.login_all(mode)
res = asyncio.run(L.run_load(duration=45, users=L.students(users), mix=L.MIX_READ, seed=rep,
                             rate=lambda t: spike_rate if 10 <= t < 30 else base_rate))
print(json.dumps({"records": [r.__dict__ for r in res.records], "alerts": res.alerts}))
