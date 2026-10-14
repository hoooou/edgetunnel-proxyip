#!/usr/bin/env python3
"""One-off probe #2: find CF-hosted endpoints usable for download/upload throughput through ProxyIP."""
import copy, json, os, subprocess, time, urllib.request
import yaml

SUB = os.environ["SUB_URL"]
API = "http://127.0.0.1:19090"
UA = {"User-Agent": "Mozilla/5.0 edgetunnel-proxyip-screen"}
sub = yaml.safe_load(urllib.request.urlopen(urllib.request.Request(SUB + "&target=clash", headers=UA), timeout=60).read())
base = copy.deepcopy(sub["proxies"][0])
ips = open("proxyip/us.txt").read().strip().split(",")[:2]
ps = []
for ip in ips:
    p = copy.deepcopy(base); p["name"] = ip; p["xhttp-opts"]["path"] = f"/proxyip={ip}"; ps.append(p)
cfg = {"mixed-port": 17890, "external-controller": "127.0.0.1:19090", "log-level": "warning", "mode": "rule",
       "dns": {"enable": False}, "proxies": ps,
       "proxy-groups": [{"name": "G", "type": "select", "proxies": [p["name"] for p in ps]}], "rules": ["MATCH,G"]}
os.makedirs("/tmp/pb", exist_ok=True)
yaml.safe_dump(cfg, open("/tmp/pb/config.yaml", "w"))
open("/tmp/up.bin", "wb").write(os.urandom(8_000_000))

DOWN = [
    "https://registry.npmjs.org/typescript/-/typescript-5.6.3.tgz",
    "https://registry.npmjs.org/next/-/next-14.2.15.tgz",
    "https://registry.npmjs.org/@next/swc-linux-x64-gnu/-/swc-linux-x64-gnu-14.2.15.tgz",
    "https://cdn.jsdelivr.net/npm/@next/swc-linux-x64-gnu@14.2.15/next-swc.linux-x64-gnu.node",
]
UP = [
    "https://registry.npmjs.org/-/npm/v1/security/audits/quick",
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://cdnjs.cloudflare.com/",
    "https://claude.ai/api/",
    "https://chatgpt.com/backend-anon/",
    "https://api.x.com/1.1/",
    "https://discord.com/api/v10/",
]


def curl(args, proxy, t=30):
    cmd = ["curl", "-s", "-o", "/dev/null", "-m", str(t)] + (["-x", "http://127.0.0.1:17890"] if proxy else []) + args
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def cfray(u):
    h = subprocess.run(["curl", "-sI", "-m", "10", u], capture_output=True, text=True).stdout.lower()
    return ("cf-ray" in h), [l for l in h.splitlines() if l.startswith(("server:", "content-length:"))]


for u in DOWN + UP:
    print("host", u, cfray(u), flush=True)


def run(tag, proxy):
    for u in DOWN:
        print(tag, "down", u.rsplit("/", 1)[-1][:40], curl(["-w", "%{http_code} %{size_download}B %{speed_download}B/s %{time_total}s", u], proxy), flush=True)
    for u in UP:
        print(tag, "up", u, curl(["-X", "POST", "-H", "Content-Type: application/octet-stream", "--data-binary", "@/tmp/up.bin",
                                  "-w", "%{http_code} %{size_upload}B %{speed_upload}B/s %{time_total}s", u], proxy, 25), flush=True)


run("direct", False)
m = subprocess.Popen(["./mihomo", "-d", "/tmp/pb"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
time.sleep(3)
for ip in ips:
    urllib.request.urlopen(urllib.request.Request(API + "/proxies/G", data=json.dumps({"name": ip}).encode(), method="PUT"))
    run("via " + ip, True)
m.kill()
