#!/usr/bin/env python3
"""Screen public ProxyIPs through your own EdgeTunnel Worker and write the best ones to a txt file.

Each candidate becomes a VLESS node whose path is /proxyip=<ip[:port]>, all sharing one entry IP,
so only the ProxyIP leg differs. mihomo runs locally and its delay API is used to probe each node
against a Cloudflare-hosted URL (only CF-hosted targets go through ProxyIP).

Stages:
  1. one probe per candidate (low concurrency), keep the fastest STAGE2 candidates
  2. ROUNDS2 probes each, score = median + 0.3 * max (penalises spikes), keep the top TOP_N
  3. top SITE_POOL must pass real requests to each SITES url
  4. survivors are tested one by one for download and upload throughput through the ProxyIP
     (BW_ROUNDS each, worst round counts), must clear MIN_DOWN / MIN_UP, ranked by
     sqrt(down*up) with a small latency penalty; best TOP_N are written

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
# Throughput targets must be Cloudflare-hosted, otherwise the Worker connects directly and the
# ProxyIP is never on the path. speed.cloudflare.com is answered by the Worker itself (204), so
# it measures nothing. registry.npmjs.org serves large tarballs from CF; cdnjs.cloudflare.com
# accepts and discards a POST body, which gives an upload sink.
DOWN_URL = os.environ.get("DOWN_URL", "https://registry.npmjs.org/next/-/next-14.2.15.tgz")  # ~20 MB
UP_URL = os.environ.get("UP_URL", "https://cdnjs.cloudflare.com/")
UP_BYTES = int(os.environ.get("UP_BYTES", str(8_000_000)))
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
SITE_POOL = int(os.environ.get("SITE_POOL", "20"))   # stage-3/4 pool size
BW_ROUNDS = int(os.environ.get("BW_ROUNDS", "2"))
MIN_DOWN = float(os.environ.get("MIN_DOWN", "20"))   # Mbps floor (runner-measured)
MIN_UP = float(os.environ.get("MIN_UP", "5"))
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


def throughput(direction):
    """Mbps for one transfer through the selected node, or None on failure."""
    if direction == "down":
        cmd = ["curl", "-s", "-o", "/dev/null", "-m", "20", "-x", f"http://127.0.0.1:{PORT}",
               "-w", "%{http_code} %{size_download} %{speed_download}", DOWN_URL]
    else:
        cmd = ["curl", "-s", "-o", "/dev/null", "-m", "20", "-x", f"http://127.0.0.1:{PORT}", "-X", "POST",
               "-H", "Content-Type: application/octet-stream", "--data-binary", f"@{WORK}/up.bin",
               "-w", "%{http_code} %{size_upload} %{speed_upload}", UP_URL]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
    if len(out) != 3 or out[0] == "000":
        return None
    size, speed = int(out[1]), float(out[2])
    want = 5_000_000 if direction == "down" else UP_BYTES
    if size < want * 0.9:
        return None
    return round(speed * 8 / 1e6, 1)


def main():
    node, opts_key = base_node()
    entry = os.environ.get("ENTRY_IP") or None
    cand = candidates()
    items = list(cand.items())
    if STAGE1_LIMIT:
        items = items[:STAGE1_LIMIT]
    os.makedirs(WORK, exist_ok=True)
    with open(f"{WORK}/up.bin", "wb") as f:
        f.write(os.urandom(UP_BYTES))
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

    # Stage 3: real site requests; everyone in the top SITE_POOL who passes moves on
    passed = []
    with Mihomo():
        for score, med, mx, n in scored[:SITE_POOL]:
            select(n)
            results = [site_ok(u) for u in SITES]
            if all(ok for ok, _ in results):
                passed.append((n, med, mx, [round(t, 2) for _, t in results]))
            else:
                log(f"drop {n}: site check failed {results}")
    log(f"stage3: {len(passed)}/{min(SITE_POOL, len(scored))} passed site checks")

    # Stage 4: throughput, one node at a time (no shared-link contention)
    final = []
    with Mihomo():
        for n, med, mx, sites in passed:
            select(n)
            down = [x for x in (throughput("down") for _ in range(BW_ROUNDS)) if x is not None]
            up = [x for x in (throughput("up") for _ in range(BW_ROUNDS)) if x is not None]
            if len(down) < BW_ROUNDS or len(up) < BW_ROUNDS:
                log(f"drop {n}: bandwidth test failed down={down} up={up}")
                continue
            d, u = min(down), min(up)  # worst of the rounds: stable beats bursty
            if d < MIN_DOWN or u < MIN_UP:
                log(f"drop {n}: down={d} up={u} Mbps below floor {MIN_DOWN}/{MIN_UP}")
                continue
            final.append((n, med, mx, sites, d, u))
            log(f"bw {n} med={med} down={d} up={u} Mbps sites={sites}")
    # Rank: bandwidth first (geometric mean of down/up, so one weak direction drags it down),
    # latency as a tie-breaker (every 100 ms costs 10%).
    final.sort(key=lambda r: -((r[4] * r[5]) ** 0.5) * (1 - min(r[1], 900) / 1000))
    final = final[:TOP_N]
    for n, med, mx, sites, d, u in final:
        log(f"keep {n} med={med} max={mx} down={d} up={u} Mbps sites={sites}")
    if len(final) < max(2, TOP_N // 2):
        sys.exit(f"only {len(final)} passed; keeping previous list")

    ips = [n.split("|", 1)[1] for n, *_ in final]
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    open(OUT, "w").write(",".join(ips) + "\n")
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SOURCE, "probe": PROBE, "sites": SITES, "country": COUNTRY, "colos": sorted(COLOS),
        "tested": len(items), "alive": len(alive),
        "down_url": DOWN_URL, "up_url": UP_URL, "min_down_mbps": MIN_DOWN, "min_up_mbps": MIN_UP,
        "results": [{"proxyip": n.split("|", 1)[1], "colo": n.split("|", 1)[0], "delay_median_ms": med,
                     "delay_max_ms": mx, "site_ttfb_s": s, "down_mbps": d, "up_mbps": u}
                    for n, med, mx, s, d, u in final],
    }
    json.dump(report, open(os.path.splitext(OUT)[0] + ".json", "w"), indent=2)
    log("wrote", OUT, ",".join(ips))


if __name__ == "__main__":
    main()
