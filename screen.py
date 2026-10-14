#!/usr/bin/env python3
"""Screen public ProxyIPs through your own EdgeTunnel Worker and write the best ones to a txt file.

Each candidate becomes a VLESS node whose path is /proxyip=<ip[:port]>, all sharing one entry IP,
so only the ProxyIP leg differs. mihomo runs locally and its delay API is used to probe each node
against a Cloudflare-hosted URL (only CF-hosted targets go through ProxyIP).

Stages:
  1. one probe per candidate (low concurrency), keep the fastest STAGE2 candidates
  2. ROUNDS2 probes each, score = median + 0.3 * max (penalises spikes), keep the top TOP_N
  3. every keeper must also pass real requests to each SITES url

Environment:
  SUB_URL       required  your EdgeTunnel subscription link (clash format)
  COUNTRY       default US
  COLOS         default SJC,LAX,SEA,PDX      CF colos reported by the candidate list
  ENTRY_IP      default: first subscription node's server
  TOP_N         default 6
  OUT           default proxyip/us.txt
  MIHOMO        default ./mihomo
"""
from __future__ import annotations

import copy
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import concurrent.futures as cf

import yaml

SOURCE = "https://zip.cm.edu.kg.cmliussss.net/all.json"
PROBE = "https://claude.ai/cdn-cgi/trace"
SITES = [
    "https://chatgpt.com/backend-anon/me",
    "https://api.x.com/1.1/hashflags.json",
    "https://x.com/",
]
API = "http://127.0.0.1:19090"
PORT = 17890
UA = {"User-Agent": "Mozilla/5.0 edgetunnel-proxyip-screen"}

SUB_URL = os.environ["SUB_URL"]
COUNTRY = os.environ.get("COUNTRY", "US")
COLOS = set(os.environ.get("COLOS", "SJC,LAX,SEA,PDX").split(","))
TOP_N = int(os.environ.get("TOP_N", "6"))
OUT = os.environ.get("OUT", "proxyip/us.txt")
MIHOMO = os.environ.get("MIHOMO", "./mihomo")
STAGE1_CONC = int(os.environ.get("STAGE1_CONC", "6"))  # >=8 inflates delays on a single link
STAGE1_LIMIT = int(os.environ.get("STAGE1_LIMIT", "0"))  # 0 = all
STAGE2 = int(os.environ.get("STAGE2", "40"))
ROUNDS2 = int(os.environ.get("ROUNDS2", "6"))
WORK = os.environ.get("WORK", "/tmp/pxscreen")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fetch(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def candidates():
    data = json.loads(fetch(SOURCE))["data"]
    out = {}
    for x in data:
        m = x.get("meta") or {}
        colo = (m.get("colo") or {}).get("iata")
        if m.get("country") != COUNTRY or colo not in COLOS or not x.get("port"):
            continue
        port = x["port"][0]
        out[x["ip"] if port == 443 else f"{x['ip']}:{port}"] = colo
    return out


def base_node():
    sub = yaml.safe_load(fetch(SUB_URL + ("&" if "?" in SUB_URL else "?") + "target=clash"))
    if not isinstance(sub, dict) or not sub.get("proxies"):
        sys.exit("subscription did not return a clash profile")
    node = copy.deepcopy(sub["proxies"][0])
    if node.get("network") == "xhttp":
        opts_key = "xhttp-opts"
    elif node.get("network") == "ws":
        opts_key = "ws-opts"
    else:
        sys.exit(f"unsupported node network: {node.get('network')}")
    return node, opts_key


def write_config(names_paths, node, opts_key, entry):
    ps = []
    for name, path in names_paths:
        p = copy.deepcopy(node)
        p["name"] = name
        if entry:
            p["server"] = entry
        p[opts_key]["path"] = path
        ps.append(p)
    cfg = {
        "mixed-port": PORT, "external-controller": API.split("//")[1], "log-level": "warning",
        "mode": "rule", "dns": {"enable": False}, "proxies": ps,
        "proxy-groups": [{"name": "G", "type": "select", "proxies": [p["name"] for p in ps]}],
        "rules": ["MATCH,G"],
    }
    os.makedirs(WORK, exist_ok=True)
    yaml.safe_dump(cfg, open(f"{WORK}/config.yaml", "w"), allow_unicode=True)


class Mihomo:
    def __enter__(self):
        self.p = subprocess.Popen([MIHOMO, "-d", WORK], stdout=open(f"{WORK}/mihomo.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(80):
            try:
                urllib.request.urlopen(API + "/version", timeout=1)
                return self
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("mihomo did not start; see mihomo.log")

    def __exit__(self, *exc):
        self.p.kill()
        self.p.wait()


def delay(name, url=PROBE, timeout_ms=5000):
    q = urllib.parse.quote(name, safe="")
    u = urllib.parse.quote(url, safe="")
    try:
        r = json.load(urllib.request.urlopen(f"{API}/proxies/{q}/delay?timeout={timeout_ms}&url={u}", timeout=timeout_ms / 1000 + 3))
        return r.get("delay")
    except Exception:
        return None


def select(name):
    req = urllib.request.Request(f"{API}/proxies/G", data=json.dumps({"name": name}).encode(), method="PUT")
    urllib.request.urlopen(req, timeout=5)
    urllib.request.urlopen(urllib.request.Request(f"{API}/connections", method="DELETE"), timeout=5)


def site_ok(url):
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", "10", "-x", f"http://127.0.0.1:{PORT}",
                        "-w", "%{http_code} %{time_starttransfer}", url], capture_output=True, text=True)
    code, t = (r.stdout.split() + ["000", "0"])[:2]
    return code != "000", float(t)


def main():
    node, opts_key = base_node()
    entry = os.environ.get("ENTRY_IP") or None
    cand = candidates()
    items = list(cand.items())
    if STAGE1_LIMIT:
        items = items[:STAGE1_LIMIT]
    log(f"{len(cand)} candidates for {COUNTRY}/{sorted(COLOS)}; testing {len(items)} via entry {entry or node['server']}")
    if not items:
        sys.exit("no candidates")

    # Stage 1
    named = [(f"{colo}|{ip}", f"/proxyip={ip}") for ip, colo in items]
    write_config(named, node, opts_key, entry)
    with Mihomo():
        with cf.ThreadPoolExecutor(STAGE1_CONC) as ex:
            d1 = dict(zip([n for n, _ in named], ex.map(delay, [n for n, _ in named])))
    alive = sorted((v, n) for n, v in d1.items() if v)
    log(f"stage1: {len(alive)}/{len(named)} alive")
    if not alive:
        sys.exit("stage1: nothing alive (entry IP or Worker broken?)")

    # Stage 2
    keep = [n for _, n in alive[:STAGE2]]
    write_config([(n, f"/proxyip={n.split('|', 1)[1]}") for n in keep], node, opts_key, entry)
    samples = {n: [] for n in keep}
    with Mihomo():
        for r in range(ROUNDS2):
            with cf.ThreadPoolExecutor(4) as ex:
                for n, v in zip(keep, ex.map(delay, keep)):
                    samples[n].append(v)
            time.sleep(3)
    scored = []
    for n, v in samples.items():
        if all(v):
            scored.append((statistics.median(v) + 0.3 * max(v), statistics.median(v), max(v), n))
    scored.sort()
    log(f"stage2: {len(scored)}/{len(keep)} passed all {ROUNDS2} rounds")

    # Stage 3: real site requests on the top few
    final = []
    with Mihomo():
        for score, med, mx, n in scored[: TOP_N * 2]:
            select(n)
            results = [site_ok(u) for u in SITES]
            if all(ok for ok, _ in results):
                final.append((n, med, mx, [round(t, 2) for _, t in results]))
                log(f"keep {n} med={med} max={mx} sites={[round(t, 2) for _, t in results]}")
            else:
                log(f"drop {n}: site check failed {results}")
            if len(final) >= TOP_N:
                break
    if len(final) < max(2, TOP_N // 2):
        sys.exit(f"only {len(final)} passed; keeping previous list")

    ips = [n.split("|", 1)[1] for n, *_ in final]
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    open(OUT, "w").write(",".join(ips) + "\n")
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SOURCE, "probe": PROBE, "sites": SITES, "country": COUNTRY, "colos": sorted(COLOS),
        "tested": len(items), "alive": len(alive),
        "results": [{"proxyip": n.split("|", 1)[1], "colo": n.split("|", 1)[0], "delay_median_ms": med,
                     "delay_max_ms": mx, "site_ttfb_s": s} for n, med, mx, s in final],
    }
    json.dump(report, open(os.path.splitext(OUT)[0] + ".json", "w"), indent=2)
    log("wrote", OUT, ",".join(ips))


if __name__ == "__main__":
    main()
