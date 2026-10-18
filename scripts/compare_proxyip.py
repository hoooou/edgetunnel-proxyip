#!/usr/bin/env python3
"""One-off A/B: the same Worker, same entry, two PROXYIP values.

  apac   -> /proxyip=apac.proxyip.<zone>      (our generated TXT list)
  list   -> /proxyip=<eight public domains>   (what the user says works)

Prints x.com status, claude.ai exit, a real X video download and a repeated-request flakiness
count for each. No secrets are printed.
"""
import copy
import json
import os
import subprocess
import time
import urllib.request

import yaml

SUB = os.environ["SUB_URL"]
PUBLIC = os.environ["PUBLIC_LIST"]
APAC = os.environ["APAC_DOMAIN"]
API = "http://127.0.0.1:19090"
PORT = 17890
video = os.environ["VIDEO_URL"]


def fetch(url):
    # the public subscription converter occasionally answers 502/504
    for i in range(5):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                          timeout=60).read()
        except Exception as e:
            print("fetch retry", i, getattr(e, "code", e.__class__.__name__), flush=True)
            time.sleep(10 * (i + 1))
    raise SystemExit("subscription unavailable")


def build():
    sub = yaml.safe_load(fetch(SUB + "&target=clash"))
    base = copy.deepcopy(sub["proxies"][0])
    ps = []
    for name, path in [("apac", f"/proxyip={APAC}"), ("list", f"/proxyip={PUBLIC}")]:
        p = copy.deepcopy(base)
        p["name"] = name
        key = "xhttp-opts" if p.get("network") == "xhttp" else "ws-opts"
        p[key]["path"] = path
        ps.append(p)
    cfg = {"mixed-port": PORT, "external-controller": API.split("//")[1], "log-level": "warning",
           "mode": "rule", "dns": {"enable": False}, "proxies": ps,
           "proxy-groups": [{"name": "G", "type": "select", "proxies": ["apac", "list"]}],
           "rules": ["MATCH,G"]}
    os.makedirs("/tmp/ab", exist_ok=True)
    yaml.safe_dump(cfg, open("/tmp/ab/config.yaml", "w"))


def select(name):
    urllib.request.urlopen(urllib.request.Request(API + "/proxies/G", data=json.dumps({"name": name}).encode(),
                                                  method="PUT"), timeout=5)


def curl(url, args, t=20):
    return subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", str(t), "-x", f"http://127.0.0.1:{PORT}",
                           "-A", "Mozilla/5.0 (Linux; Android 14) Chrome/129"] + args + ["-w", "%{http_code} %{time_starttransfer} %{speed_download}", url],
                          capture_output=True, text=True).stdout.split()


def main():
    build()
    print("entry:", yaml.safe_load(open("/tmp/ab/config.yaml"))["proxies"][0]["server"])
    m = subprocess.Popen(["./mihomo", "-d", "/tmp/ab"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(3)
    try:
        for name in ("apac", "list"):
            select(name)
            print(f"\n=== {name}")
            # exit address as a CF site sees it
            r = subprocess.run(["curl", "-s", "-m", "15", "-x", f"http://127.0.0.1:{PORT}",
                                "https://claude.ai/cdn-cgi/trace"], capture_output=True, text=True).stdout
            kv = dict(l.split("=", 1) for l in r.splitlines() if "=" in l)
            print("  exit:", kv.get("ip"), kv.get("loc"))
            # flakiness: 6 x.com requests
            ok = 0
            for i in range(6):
                c = curl("https://x.com/", ["-o", "/dev/null"])
                ok += 1 if c and c[0] == "200" else 0
            print(f"  x.com 200: {ok}/6")
            # real video, 1.5 MB range
            v = curl(video, ["-r", "0-1500000"], 25)
            if v and v[0] in ("200", "206"):
                print(f"  video: {int(v[2]) * 8 / 1e6:.1f} Mbps, first byte {float(v[1]):.2f}s")
            else:
                print("  video: FAILED", v)
    finally:
        m.kill()


if __name__ == "__main__":
    main()
