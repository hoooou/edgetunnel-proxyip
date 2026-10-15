#!/usr/bin/env python3
"""Find residential (broadband ISP) ProxyIPs in Five Eyes / Western Europe and write the usable ones.

Pipeline (each step only narrows the list; every claim is backed by a lookup or a live test):
  1. candidates: zip.cm.edu.kg list, country in COUNTRIES, recorded egress is IPv4
  2. ip-api.com batch (free, no key): drop hosting=True and proxy=True
  3. ipapi.is with IPAPI_IS_KEY: keep is_datacenter=False and company.type == "isp";
     purity = 100 * (1 - company.abuser_score); drop is_abuser / is_vpn / is_tor.
     Results are cached in proxyip/res-cache.json for CACHE_DAYS so the daily quota is not
     spent on the same IPs again.
  4. through the Worker: egress must be the ProxyIP itself (IPv4, not WARP/CF), x.com 200,
     api.x.com not 403, then download/upload throughput.
  5. rank by purity, then sqrt(down*up) with a latency penalty; write OUT and OUT.json.

"Residential" here means: ipapi.is says the owning company is an ISP and the range is not a
datacenter. It does not prove a home connection, and it says nothing about stability.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.request
import concurrent.futures as cf

import screen as s  # reuse node building, mihomo control, probes and throughput

FIVE_EYES = "US,CA,GB,AU,NZ"
WEST_EU = "IE,DE,NL,FR,BE,LU,CH,AT,IT,ES,PT,DK,SE,NO,FI,IS"
COUNTRIES = set(os.environ.get("RES_COUNTRIES", FIVE_EYES + "," + WEST_EU).split(","))
OUT = os.environ.get("RES_OUT", "proxyip/res.txt")
CACHE = os.environ.get("RES_CACHE", "proxyip/res-cache.json")
CACHE_DAYS = int(os.environ.get("RES_CACHE_DAYS", "7"))
IPAPI_KEY = os.environ.get("IPAPI_IS_KEY", "")
IPAPI_BUDGET = int(os.environ.get("IPAPI_BUDGET", "900"))  # one run/day; free tier is 1000 lookups/day
MIN_PURITY = float(os.environ.get("MIN_PURITY", "90"))
TOP_N = int(os.environ.get("RES_TOP_N", "8"))
MIN_DOWN = float(os.environ.get("RES_MIN_DOWN", "5"))
MIN_UP = float(os.environ.get("RES_MIN_UP", "2"))

log = s.log


def candidates():
    data = json.loads(s.fetch(s.SOURCE))["data"]
    out = {}
    for x in data:
        m = x.get("meta") or {}
        if m.get("country") not in COUNTRIES or not x.get("port"):
            continue
        if ":" in (m.get("clientIp") or ":"):
            continue
        port = x["port"][0]
        out[x["ip"]] = {"addr": x["ip"] if port == 443 else f"{x['ip']}:{port}", "country": m.get("country"),
                        "colo": (m.get("colo") or {}).get("iata"), "asn": m.get("asn"), "org": m.get("asOrganization")}
    return out


def ip_api(ips):
    """ip-api.com batch: 100 per request, 15 requests/min without a key."""
    res = {}
    for i in range(0, len(ips), 100):
        body = json.dumps([{"query": ip, "fields": "query,status,isp,as,hosting,proxy,mobile"} for ip in ips[i:i + 100]]).encode()
        req = urllib.request.Request("http://ip-api.com/batch", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                left, ttl = r.headers.get("X-Rl"), r.headers.get("X-Ttl")
                for x in json.load(r):
                    if x.get("status") == "success":
                        res[x["query"]] = x
        except Exception as e:
            log(f"ip-api batch failed: {e.__class__.__name__}")
            left, ttl = "0", "60"
        if left is not None and int(left) <= 1:
            time.sleep(int(ttl or 60) + 1)
    return res


def load_cache():
    try:
        c = json.load(open(CACHE))
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - CACHE_DAYS * 86400
    return {ip: v for ip, v in c.items() if v.get("checked", 0) >= cutoff}


def ipapi_is(ips, cache):
    """Fill cache with ipapi.is verdicts for ips not cached yet (bounded by IPAPI_BUDGET)."""
    todo = [ip for ip in ips if ip not in cache][:IPAPI_BUDGET]
    if not IPAPI_KEY:
        log("IPAPI_IS_KEY not set: residential/purity stays unknown, nothing will be published")
        return
    log(f"ipapi.is: {len(ips)} need a verdict, {len(ips) - len([i for i in ips if i not in cache])} cached, querying {len(todo)}")
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        req = urllib.request.Request("https://api.ipapi.is/", method="POST",
                                     data=json.dumps({"ips": batch, "key": IPAPI_KEY}).encode(),
                                     headers={"Content-Type": "application/json", **s.UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                vals = json.load(r)
        except urllib.error.HTTPError as e:
            log(f"ipapi.is HTTP {e.code}: stopping lookups for this run")  # never log the key/request
            return
        except Exception as e:
            log(f"ipapi.is batch failed: {e.__class__.__name__}")
            continue
        for ip in batch:
            v = vals.get(ip) if isinstance(vals, dict) else None
            if not isinstance(v, dict) or not isinstance(v.get("company"), dict):
                continue
            comp = v["company"]
            try:
                ratio = float(str(comp.get("abuser_score", "")).split()[0])
            except (ValueError, IndexError):
                ratio = None
            cache[ip] = {
                "checked": int(time.time()),
                "company_type": comp.get("type"), "company": comp.get("name"),
                "asn_type": (v.get("asn") or {}).get("type"),
                "is_datacenter": v.get("is_datacenter"), "is_mobile": v.get("is_mobile"),
                "is_proxy": v.get("is_proxy"), "is_vpn": v.get("is_vpn"), "is_tor": v.get("is_tor"),
                "is_abuser": v.get("is_abuser"),
                "purity": round(100 * (1 - ratio), 2) if ratio is not None and 0 <= ratio <= 1 else None,
            }


def residential(v):
    return (v.get("is_datacenter") is False and v.get("company_type") == "isp"
            and not v.get("is_vpn") and not v.get("is_tor") and not v.get("is_abuser")
            and v.get("purity") is not None and v["purity"] >= MIN_PURITY)


def main():
    cand = candidates()
    log(f"{len(cand)} IPv4-egress candidates in {len(COUNTRIES)} countries")

    lite = ip_api(sorted(cand))
    step2 = [ip for ip, x in lite.items() if x.get("hosting") is False and x.get("proxy") is False]
    log(f"ip-api: {len(lite)} answered, {len(step2)} not hosting/proxy")

    cache = load_cache()
    ipapi_is(step2, cache)
    os.makedirs(os.path.dirname(CACHE) or ".", exist_ok=True)
    json.dump(cache, open(CACHE, "w"), indent=1, sort_keys=True)
    res = [ip for ip in step2 if ip in cache and residential(cache[ip])]
    log(f"ipapi.is: {len([i for i in step2 if i in cache])} verdicts, {len(res)} residential with purity >= {MIN_PURITY}")
    if not res:
        sys.exit("no residential candidates; keeping previous list")

    # Live tests through the Worker
    node, opts_key = s.base_node()
    entry = os.environ.get("ENTRY_IP") or None
    names = [(f"{cand[ip]['country']}|{cand[ip]['addr']}", f"/proxyip={cand[ip]['addr']}") for ip in res]
    s.write_config(names, node, opts_key, entry)
    os.makedirs(s.WORK, exist_ok=True)
    with open(f"{s.WORK}/up.bin", "wb") as f:
        f.write(os.urandom(s.UP_BYTES))
    final = []
    with s.Mihomo():
        with cf.ThreadPoolExecutor(4) as ex:
            d = dict(zip([n for n, _ in names], ex.map(s.delay, [n for n, _ in names])))
        for n, _ in names:
            ip = n.split("|", 1)[1].split(":")[0]
            if not d.get(n):
                log(f"drop {n}: unreachable through Worker")
                continue
            s.select(n)
            seen, warp = s.egress()
            if seen != ip or warp == "on":
                log(f"drop {n}: egress {seen or '?'} is not the ProxyIP itself")
                continue
            sites = [s.site_ok(u, acc) for u, acc in s.SITES]
            if not all(ok for ok, _, _ in sites):
                log(f"drop {n}: site check {[c for _, _, c in sites]}")
                continue
            down, up = s.throughput("down"), s.throughput("up")
            if down is None or up is None or down < MIN_DOWN or up < MIN_UP:
                log(f"drop {n}: bandwidth down={down} up={up}")
                continue
            v = cache[ip]
            final.append({"proxyip": n.split("|", 1)[1], "country": cand[ip]["country"], "colo": cand[ip]["colo"],
                          "company": v["company"], "purity": v["purity"], "is_mobile": v["is_mobile"],
                          "delay_ms": d[n], "down_mbps": down, "up_mbps": up,
                          "site_ttfb_s": [round(t, 2) for _, t, _ in sites]})
            log(f"ok {n} {v['company']} purity={v['purity']} delay={d[n]} down={down} up={up}")

    final.sort(key=lambda r: (-r["purity"], -((r["down_mbps"] * r["up_mbps"]) ** 0.5) * (1 - min(r["delay_ms"], 900) / 1000)))
    final = final[:TOP_N]
    if len(final) < 2:
        sys.exit(f"only {len(final)} usable residential ProxyIPs; keeping previous list")
    open(OUT, "w").write(",".join(r["proxyip"] for r in final) + "\n")
    json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "definition": "ipapi.is company.type=isp and is_datacenter=false; purity=100*(1-company.abuser_score)",
               "countries": sorted(COUNTRIES), "min_purity": MIN_PURITY, "candidates": len(cand),
               "not_hosting": len(step2), "residential": len(res), "results": final},
              open(os.path.splitext(OUT)[0] + ".json", "w"), indent=2, ensure_ascii=False)
    log("wrote", OUT, ",".join(r["proxyip"] for r in final))


if __name__ == "__main__":
    main()
