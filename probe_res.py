#!/usr/bin/env python3
"""One-off: are the 19 ISP-looking ProxyIPs usable through the Worker (Placement=SJC)?"""
import copy, json, os, subprocess, time, urllib.parse, urllib.request
import yaml

SUB = os.environ["SUB_URL"]
API = "http://127.0.0.1:19090"
UA = {"User-Agent": "Mozilla/5.0 edgetunnel-proxyip-screen"}
BUA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36"
IPS = ["172.117.126.136", "50.35.41.6", "71.168.153.36", "204.93.50.147", "174.108.80.25:8443", "24.136.24.254:2083",
       "70.79.240.221", "142.188.90.91", "24.78.21.213", "24.141.96.92:8443", "64.58.46.192:8443",
       "114.34.210.159", "114.32.43.140:2087", "119.205.235.58", "123.111.169.70",
       "62.251.108.251", "31.201.109.180", "51.6.54.142", "2.7.113.69"]
sub = yaml.safe_load(urllib.request.urlopen(urllib.request.Request(SUB + "&target=clash", headers=UA), timeout=60).read())
base = copy.deepcopy(sub["proxies"][0])
ps = []
for ip in IPS:
    p = copy.deepcopy(base); p["name"] = ip; p["xhttp-opts"]["path"] = f"/proxyip={ip}"; ps.append(p)
cfg = {"mixed-port": 17890, "external-controller": "127.0.0.1:19090", "log-level": "warning", "mode": "rule",
       "dns": {"enable": False}, "proxies": ps,
       "proxy-groups": [{"name": "G", "type": "select", "proxies": IPS}], "rules": ["MATCH,G"]}
os.makedirs("/tmp/pr", exist_ok=True)
yaml.safe_dump(cfg, open("/tmp/pr/config.yaml", "w"))
open("/tmp/up.bin", "wb").write(os.urandom(4_000_000))


def c(args, t=12):
    return subprocess.run(["curl", "-s", "-m", str(t), "-x", "http://127.0.0.1:17890", "-A", BUA] + args,
                          capture_output=True, text=True).stdout


m = subprocess.Popen(["./mihomo", "-d", "/tmp/pr"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
time.sleep(3)
for ip in IPS:
    urllib.request.urlopen(urllib.request.Request(API + "/proxies/G", data=json.dumps({"name": ip}).encode(), method="PUT"))
    q = urllib.parse.quote(ip, safe="") if False else None
    try:
        d = json.load(urllib.request.urlopen(f"{API}/proxies/{urllib.parse.quote(ip, safe='')}/delay?timeout=5000&url=https%3A%2F%2Fclaude.ai%2Fcdn-cgi%2Ftrace", timeout=8)).get("delay")
    except Exception:
        d = None
    tr = dict(l.split("=", 1) for l in c(["https://x.com/cdn-cgi/trace"]).splitlines() if "=" in l)
    x = c(["-o", "/dev/null", "-w", "%{http_code}", "https://x.com/"])
    dn = c(["-o", "/dev/null", "-w", "%{http_code} %{speed_download}", "https://registry.npmjs.org/next/-/next-14.2.15.tgz"], 20).split()
    up = c(["-o", "/dev/null", "-X", "POST", "-H", "Content-Type: application/octet-stream", "--data-binary", "@/tmp/up.bin",
            "-w", "%{http_code} %{speed_upload}", "https://cdnjs.cloudflare.com/"], 20).split()
    mb = lambda v: round(float(v[1]) * 8 / 1e6, 1) if len(v) == 2 and v[0] != "000" else None
    print(f"RES {ip:20s} delay={d} exit={tr.get('ip','-')} loc={tr.get('loc','-')} warp={tr.get('warp','-')} x={x} down={mb(dn)} up={mb(up)}", flush=True)
m.kill()
