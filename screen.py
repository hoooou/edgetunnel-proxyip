#!/usr/bin/env python3
"""Screen public ProxyIPs through your own EdgeTunnel Worker and write the best ones to a txt file.

Each candidate becomes a VLESS node whose path is /proxyip=<ip[:port]>, all sharing one entry IP,
so only the ProxyIP leg differs. mihomo runs locally and its delay API is used to probe each node
against a Cloudflare-hosted URL (only CF-hosted targets go through ProxyIP).

Stages:
  1. one probe per candidate (low concurrency), keep the fastest STAGE2 candidates
  2. ROUNDS2 probes each, score = median + 0.3 * max (penalises spikes), keep the top TOP_N
  3. top SITE_POOL: egress (as seen by a CF site) must be IPv4, not WARP, not Cloudflare-owned;
     x.com must return 200 and api.x.com must not be refused (403); ChatGPT must be reachable
  4. survivors are tested one by one for download and upload throughput through the ProxyIP
     (BW_ROUNDS each, worst round counts), must clear MIN_DOWN / MIN_UP, ranked by
     sqrt(down*up) with a small latency penalty; best TOP_N are written

Environment:
  SUB_URL       required  your EdgeTunnel subscription link (clash format)
  COUNTRY       default US          one or more, comma separated (JP,SG)
  COLOS         default SJC,LAX,SEA,PDX      CF colos reported by the candidate list
  PER_COUNTRY   optional quota in the final list, e.g. JP:4,SG:4
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
import ipaddress

import yaml

SOURCE = "https://zip.cm.edu.kg.cmliussss.net/all.json"
PROBE = "https://claude.ai/cdn-cgi/trace"
# Throughput targets must be Cloudflare-hosted, otherwise the Worker connects directly and the
# ProxyIP is never on the path. speed.cloudflare.com is answered by the Worker itself (204), so
# it measures nothing. registry.npmjs.org serves large tarballs from CF; cdnjs.cloudflare.com
# accepts and discards a POST body, which gives an upload sink.
# Small payloads on purpose: throughput over a proxy is measurable in a couple of seconds,
# and short transfers keep the whole run fast. Both targets are Cloudflare-hosted so the
# request really goes through the ProxyIP.
DOWN_URL = os.environ.get("DOWN_URL", "https://registry.npmjs.org/typescript/-/typescript-5.6.3.tgz")  # ~4 MB
DOWN_BYTES = int(os.environ.get("DOWN_BYTES", str(4_000_000)))
UP_URL = os.environ.get("UP_URL", "https://cdnjs.cloudflare.com/")
UP_BYTES = int(os.environ.get("UP_BYTES", str(3_000_000)))
# Each site must answer with an accepted status. "Connected" is not enough: X answers 403 with a
# body of "IPv6" when the egress is an IPv6 address it does not accept, and CF's bot wall answers
# 403 with a cf-mitigated header. ChatGPT's anonymous endpoint is 403/cf-mitigated from any
# datacenter IP, so it only checks reachability, not acceptance.
SITES = [
    ("https://x.com/", {200}),
    ("https://api.x.com/1.1/hashflags.json", {200, 400, 401}),
    ("https://chatgpt.com/backend-anon/me", None),
]
TRACE = "https://x.com/cdn-cgi/trace"
UA_BROWSER = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"
REQUIRE_V4 = os.environ.get("REQUIRE_V4", "1") == "1"
API = "http://127.0.0.1:19090"
PORT = 17890
UA = {"User-Agent": "Mozilla/5.0 edgetunnel-proxyip-screen"}

SUB_URL = os.environ["SUB_URL"]
COUNTRY = os.environ.get("COUNTRY", "US")          # one or more, comma separated (e.g. JP,SG)
COUNTRIES = set(COUNTRY.split(","))
COLOS = set(os.environ.get("COLOS", "SJC,LAX,SEA,PDX").split(","))
# Optional per-country quota for the final list, e.g. "JP:4,SG:4". Empty = no quota.
PER_COUNTRY = {k: int(v) for k, v in (x.split(":") for x in os.environ.get("PER_COUNTRY", "").split(",") if x)}
TOP_N = int(os.environ.get("TOP_N", "6"))
OUT = os.environ.get("OUT", "proxyip/us.txt")
MIHOMO = os.environ.get("MIHOMO", "./mihomo")
STAGE1_CONC = int(os.environ.get("STAGE1_CONC", "6"))  # >=8 inflates delays on a single link
STAGE1_LIMIT = int(os.environ.get("STAGE1_LIMIT", "0"))  # 0 = all
STAGE2 = int(os.environ.get("STAGE2", "40"))
ROUNDS2 = int(os.environ.get("ROUNDS2", "4"))
SITE_POOL = int(os.environ.get("SITE_POOL", "20"))   # stage-3/4 pool size
CONFIRM_POOL = int(os.environ.get("CONFIRM_POOL", "16"))  # single-stream confirmation pool
BW_ROUNDS = int(os.environ.get("BW_ROUNDS", "1"))
MIN_DOWN = float(os.environ.get("MIN_DOWN", "20"))   # Mbps floor (runner-measured)
MIN_UP = float(os.environ.get("MIN_UP", "5"))
BW_TIMEOUT = int(os.environ.get("BW_TIMEOUT", "15"))   # seconds per transfer
LISTEN_BASE = int(os.environ.get("LISTEN_BASE", "23000"))  # per-node local port for parallel checks
STAGE3_CONC = int(os.environ.get("STAGE3_CONC", "6"))
STAGE4_CONC = int(os.environ.get("STAGE4_CONC", "4"))
WORK = os.environ.get("WORK", "/tmp/pxscreen")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fetch(url, timeout=60, tries=2):
    # The subscription goes through a public converter that sometimes answers 502/504.
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise
            log(f"fetch failed ({getattr(e, 'code', e.__class__.__name__)}), retry {i + 1}")
            time.sleep(15 * (i + 1))


def candidates():
    data = json.loads(fetch(SOURCE))["data"]
    out = {}
    for x in data:
        m = x.get("meta") or {}
        colo = (m.get("colo") or {}).get("iata")
        if m.get("country") not in COUNTRIES or colo not in COLOS or not x.get("port"):
            continue
        if REQUIRE_V4 and ":" in (m.get("clientIp") or ":"):
            continue  # list was built by probing through it; its recorded egress is IPv6/unknown
        port = x["port"][0]
        # tag = colo for a single country (unchanged), "CC-colo" when several are mixed
        out[x["ip"] if port == 443 else f"{x['ip']}:{port}"] = colo if len(COUNTRIES) == 1 else f"{m['country']}-{colo}"
    return out


NODE_CACHE = os.environ.get("NODE_CACHE", "proxyip/node.enc")


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _save_node(node):
    # The node holds the UUID, and the repo is public: store it XOR'ed with a key derived from
    # SUB_URL (a secret). Not strong crypto, just keeps the credential out of plain sight.
    import hashlib, base64
    key = hashlib.sha256(SUB_URL.encode()).digest()
    os.makedirs(os.path.dirname(NODE_CACHE) or ".", exist_ok=True)
    open(NODE_CACHE, "w").write(base64.b64encode(_xor(json.dumps(node).encode(), key)).decode() + "\n")


def _load_node():
    import hashlib, base64
    key = hashlib.sha256(SUB_URL.encode()).digest()
    return json.loads(_xor(base64.b64decode(open(NODE_CACHE).read().strip()), key))


def base_node():
    """First node of the subscription. Falls back to the last good copy when the subscription
    converter (a third-party service) is down, so screening never depends on it."""
    node = None
    try:
        sub = yaml.safe_load(fetch(SUB_URL + ("&" if "?" in SUB_URL else "?") + "target=clash"))
        if isinstance(sub, dict) and sub.get("proxies"):
            node = copy.deepcopy(sub["proxies"][0])
            _save_node(node)
    except Exception as e:
        log(f"subscription unavailable ({getattr(e, 'code', e.__class__.__name__)}); using cached node")
    if node is None:
        try:
            node = _load_node()
        except (OSError, ValueError):
            sys.exit("subscription unavailable and no cached node")
    if node.get("network") == "xhttp":
        opts_key = "xhttp-opts"
    elif node.get("network") == "ws":
        opts_key = "ws-opts"
    else:
        sys.exit(f"unsupported node network: {node.get('network')}")
    node[opts_key]["path"] = "/"
    return node, opts_key


def write_config(names_paths, node, opts_key, entry, listeners=False):
    """Write the profile. With listeners=True every node also gets its own local mixed port,
    so egress/site/bandwidth checks can run concurrently instead of switching one selector."""
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
    ports = {}
    if listeners:
        cfg["listeners"] = [{"name": f"l{i}", "type": "mixed", "port": LISTEN_BASE + i,
                             "listen": "127.0.0.1", "proxy": n} for i, (n, _) in enumerate(names_paths)]
        ports = {n: LISTEN_BASE + i for i, (n, _) in enumerate(names_paths)}
    os.makedirs(WORK, exist_ok=True)
    yaml.safe_dump(cfg, open(f"{WORK}/config.yaml", "w"), allow_unicode=True)
    return ports


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


def site_ok(url, accept, port=PORT):
    """(ok, ttfb_s, status). accept=None means any HTTP response counts."""
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", "10", "-x", f"http://127.0.0.1:{port}",
                        "-A", UA_BROWSER, "-w", "%{http_code} %{time_starttransfer}", url],
                       capture_output=True, text=True)
    code, t = (r.stdout.split() + ["000", "0"])[:2]
    ok = code != "000" and (accept is None or int(code) in accept)
    return ok, float(t), code


EGRESS_TRACES = [TRACE, "https://claude.ai/cdn-cgi/trace", "https://www.cloudflare.com/cdn-cgi/trace"]


def egress(port=PORT):
    """What a CF-hosted site sees as the client: (ip, warp flag).

    Retries across several CF trace endpoints: a single empty answer (transient reset on one
    CF site) must not be read as "this ProxyIP has no egress".
    """
    for url in EGRESS_TRACES:
        for _ in range(2):
            r = subprocess.run(["curl", "-s", "-m", "10", "-x", f"http://127.0.0.1:{port}", url],
                               capture_output=True, text=True).stdout
            kv = dict(l.split("=", 1) for l in r.splitlines() if "=" in l)
            if kv.get("ip"):
                return kv["ip"], kv.get("warp", "")
    return "", ""


def is_cloudflare(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in CF_NETS)


CF_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 "
    "190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 "
    "104.24.0.0/14 104.28.0.0/16 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 "
    "2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32 2a09:bac0::/29").split()]


def throughput(direction, port=PORT):
    """Mbps for one transfer through the given local listener, or None on failure."""
    if direction == "down":
        cmd = ["curl", "-s", "-o", "/dev/null", "-m", str(BW_TIMEOUT), "-x", f"http://127.0.0.1:{port}",
               "-w", "%{http_code} %{size_download} %{speed_download}", DOWN_URL]
    else:
        cmd = ["curl", "-s", "-o", "/dev/null", "-m", str(BW_TIMEOUT), "-x", f"http://127.0.0.1:{port}", "-X", "POST",
               "-H", "Content-Type: application/octet-stream", "--data-binary", f"@{WORK}/up.bin",
               "-w", "%{http_code} %{size_upload} %{speed_upload}", UP_URL]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.split()
    if len(out) != 3 or out[0] == "000":
        return None
    size, speed = int(out[1]), float(out[2])
    want = DOWN_BYTES if direction == "down" else UP_BYTES
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

    # Stage 1: alive on two independent probes. Cheap, and it drops the flaky half before any
    # expensive test runs.
    named = [(f"{colo}|{ip}", f"/proxyip={ip}") for ip, colo in items]
    write_config(named, node, opts_key, entry)
    names1 = [n for n, _ in named]
    with Mihomo():
        with cf.ThreadPoolExecutor(STAGE1_CONC) as ex:
            first = dict(zip(names1, ex.map(delay, names1)))
        second = dict(zip(names1, ex.map(delay, names1)))
    alive = sorted((min(first[n], second[n]), n) for n in names1 if first[n] and second[n])
    log(f"stage1: {len(alive)}/{len(named)} answered both probes")
    if not alive:
        sys.exit("stage1: nothing alive (entry IP or Worker broken?)")

    # Stage 2
    keep = [n for _, n in alive[:STAGE2]]
    write_config([(n, f"/proxyip={n.split('|', 1)[1]}") for n in keep], node, opts_key, entry)
    samples = {n: [] for n in keep}
    with Mihomo():
        for r in range(ROUNDS2):
            with cf.ThreadPoolExecutor(6) as ex:
                for n, v in zip(keep, ex.map(delay, keep)):
                    samples[n].append(v)
    scored = []
    for n, v in samples.items():
        if all(v):
            scored.append((statistics.median(v) + 0.3 * max(v), statistics.median(v), max(v), n))
    scored.sort()
    log(f"stage2: {len(scored)}/{len(keep)} passed all {ROUNDS2} rounds")

    # Stage 3: egress + real site requests, in parallel (one listener per node)
    passed = []
    ports = write_config([(n, f"/proxyip={n.split('|', 1)[1]}") for _, _, _, n in scored[:SITE_POOL]],
                         node, opts_key, entry, listeners=True)
    with Mihomo():
        def check3(item):
            score, med, mx, n = item
            port = ports[n]
            ip, warp = egress(port)
            if not ip or warp == "on" or is_cloudflare(ip):
                return n, None, f"egress {ip or '?'} warp={warp} (Cloudflare/WARP exit)"
            if REQUIRE_V4 and ":" in ip:
                return n, None, f"egress is IPv6 {ip}"
            results = [site_ok(u, acc, port) for u, acc in SITES]
            if not all(ok for ok, _, _ in results):
                return n, None, f"site check {[(u.split('/')[2], c) for (u, _), (_, _, c) in zip(SITES, results)]}"
            return n, (med, mx, [round(t, 2) for _, t, _ in results]), None
        with cf.ThreadPoolExecutor(STAGE3_CONC) as ex:
            for n, ok, why in ex.map(check3, scored[:SITE_POOL]):
                if ok:
                    passed.append((n, *ok))
                else:
                    log(f"drop {n}: {why}")
    log(f"stage3: {len(passed)}/{min(SITE_POOL, len(scored))} passed site checks")

    # Stage 4: throughput in parallel (one listener per node, small payloads)
    final = []
    ports4 = write_config([(n, f"/proxyip={n.split('|', 1)[1]}") for n, _, _, _ in passed],
                          node, opts_key, entry, listeners=True)
    with Mihomo():
        def check4(item):
            n, med, mx, sites = item
            port = ports4[n]
            down = [x for x in (throughput("down", port) for _ in range(BW_ROUNDS)) if x is not None]
            up = [x for x in (throughput("up", port) for _ in range(BW_ROUNDS)) if x is not None]
            if len(down) < BW_ROUNDS or len(up) < BW_ROUNDS:
                return n, None, f"bandwidth test failed down={down} up={up}"
            d, u = min(down), min(up)  # worst of the rounds: stable beats bursty
            if d < MIN_DOWN or u < MIN_UP:
                return n, None, f"down={d} up={u} Mbps below floor {MIN_DOWN}/{MIN_UP}"
            return n, (med, mx, sites, d, u), None
        with cf.ThreadPoolExecutor(STAGE4_CONC) as ex:
            for n, ok, why in ex.map(check4, passed):
                if ok:
                    final.append((n, *ok))
                    log(f"bw {n} med={ok[0]} down={ok[3]} up={ok[4]} Mbps sites={ok[2]}")
                else:
                    log(f"drop {n}: {why}")
    # Stage 5: confirm the finalists one at a time. Stage 4 shares the runner's link between up to
    # FINAL_CONC streams, so its numbers are a screening estimate; the published order and the
    # floors are decided on a clean single stream.
    final.sort(key=lambda r: -((r[4] * r[5]) ** 0.5) * (1 - min(r[1], 900) / 1000))
    if final and CONFIRM_POOL:
        pool = final[:max(TOP_N, CONFIRM_POOL)]
        cports = write_config([(n, f"/proxyip={n.split('|', 1)[1]}") for n, *_ in pool],
                              node, opts_key, entry, listeners=True)
        confirmed = []
        with Mihomo():
            for n, med, mx, sites, d0, u0 in pool:
                port = cports[n]
                d = throughput("down", port)
                u = throughput("up", port)
                if d is None or u is None or d < MIN_DOWN or u < MIN_UP:
                    log(f"drop {n}: single-stream confirm down={d} up={u} (screened {d0}/{u0})")
                    continue
                confirmed.append((n, med, mx, sites, d, u))
                log(f"confirm {n} down={d} up={u} Mbps (screened {d0}/{u0})")
        final = confirmed

    # Rank: bandwidth first (geometric mean of down/up, so one weak direction drags it down),
    # latency as a tie-breaker (every 100 ms costs 10%).
    if PER_COUNTRY:
        # Fill each country's quota from its own best, then top up with the best of the rest.
        picked, used = [], {}
        for r in final:
            cc = r[0].split("|", 1)[0].split("-", 1)[0]
            if used.get(cc, 0) < PER_COUNTRY.get(cc, 0):
                picked.append(r); used[cc] = used.get(cc, 0) + 1
        picked += [r for r in final if r not in picked][:max(0, TOP_N - len(picked))]
        log("per-country:", {cc: used.get(cc, 0) for cc in PER_COUNTRY})
        final = picked[:TOP_N]
    else:
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
        "source": SOURCE, "probe": PROBE, "sites": [u for u, _ in SITES], "require_ipv4_egress": REQUIRE_V4, "country": COUNTRY, "per_country": PER_COUNTRY, "colos": sorted(COLOS),
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
