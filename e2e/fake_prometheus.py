"""Stand-in Prometheus for end-to-end testing of the CoRCA workspace.

Serves /api/v1/query_range for Online Boutique-style cpu and mem queries.
Scenario: two *concurrent* root causes injected at INCIDENT:
  - cartservice: CPU saturation
  - currencyservice: memory leak (ramp)
Symptoms propagate to callers (frontend, checkoutservice) with smaller CPU bumps,
so the base ranker sees correlated anomalies and CoRCA has to explain them away.
Before INCIDENT every series is noisy-but-stable (healthy window for abstention checks).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import sys
import time
from urllib.parse import parse_qs, urlsplit

import numpy as np

SERVICES = ["adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice", "frontend",
            "paymentservice", "productcatalogservice", "recommendationservice", "redis-cart", "shippingservice"]
INCIDENT = int(sys.argv[2]) if len(sys.argv) > 2 else int(time.time()) - 400
BASE_CPU = {s: 0.05 + 0.01 * i for i, s in enumerate(SERVICES)}
BASE_MEM = {s: (60 + 7 * i) * 2**20 for i, s in enumerate(SERVICES)}


def value(service, kind, t):
    rng = np.random.default_rng(abs(hash((service, kind, t))) % 2**32)
    after = t >= INCIDENT
    since = max(0, t - INCIDENT)
    if kind == "cpu":
        v = BASE_CPU[service] * (1 + rng.normal(0, 0.04))
        if after and service == "cartservice":
            v += 0.9                              # root cause 1: CPU hog
        if after and service in ("frontend", "checkoutservice"):
            v += 0.08 * (1 + rng.normal(0, 0.1))  # propagated symptom on callers
        return max(v, 0)
    v = BASE_MEM[service] * (1 + rng.normal(0, 0.005))
    if after and service == "currencyservice":
        v += min(since, 120) * 1.5 * 2**20        # root cause 2: memory leak ramp
    return v


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path != "/api/v1/query_range":
            self.send_response(404); self.end_headers(); return
        query = q["query"][0]
        kind = "mem" if "memory" in query else "cpu"
        start, end, step = (int(float(q[k][0])) for k in ("start", "end", "step"))
        result = [{"metric": {"service": s},
                   "values": [[t, f"{value(s, kind, t):.6f}"] for t in range(start, end + 1, step) if t <= time.time()]}
                  for s in SERVICES]
        body = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9090
    print(f"fake prometheus on :{port}, incident at {INCIDENT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
