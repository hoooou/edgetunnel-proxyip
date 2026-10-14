#!/usr/bin/env python3
"""One-off: test candidate upload/download endpoints directly and through the Worker with 3 ProxyIPs."""
import copy, json, os, subprocess, time, urllib.request
import yaml

SUB = os.environ["SUB_URL"]
API = "http://127.0.0.1:19090"
sub = yaml.safe_load(urllib.request.urlopen(urllib.request.Request(SUB + "&target=clash", headers={"User-Agent": "Mozilla/5.0 edgetunnel-proxyip-screen"}), timeout=60).read())
base = copy.deepcopy(sub["proxies"][0])
ips = open("proxyip/us.txt").read().strip().split(",")[:3]
ps = []
for ip in ips:
    p = copy.deepcopy(base); p["name"] = ip; p["xhttp-opts"]["path"] = f"/proxyip={ip}"; ps.append(p)
cfg = {"mixed-port": 17890, "external-controller": "127.0.0.1:19090", "log-level": "warning", "mode": "rule",
       "dns": {"enable": False}, "proxies": ps,
       "proxy-groups": [{"name": "G", "type": "select", "proxies": [p["name"] for p in ps]}], "rules": ["MATCH,G"]}
os.makedirs("/tmp/pb", exist_ok=True)
yaml.safe_dump(cfg, open("/tmp/pb/config.yaml", "w"))
open("/tmp/up.bin", "wb").write(os.urandom(10_000_000))

H = ["-H", "Referer: https://speed.cloudflare.com/", "-H", "Origin: https://speed.cloudflare.com"]
DOWN = {
    "cf_speed_25MB": "https://speed.cloudflare.com/__down?bytes=25000000",
    "cdnjs_tf": "https://cdnjs.cloudflare.com/ajax/libs/tensorflow/4.22.0/tf.min.js",
}
UP = {
    "cf_speed_up": "https://speed.cloudflare.com/__up",
    "httpbin_cf?": "https://httpbin.org/post",
}


def curl(args, proxy):
    cmd = ["curl", "-s", "-o", "/dev/null", "-m", "25"] + (["-x", "http://127.0.0.1:17890"] if proxy else []) + args
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def run(tag, proxy):
    for k, u in DOWN.items():
        print(tag, "down", k, curl(H + ["-w", "%{http_code} %{size_download}B %{speed_download}B/s", u], proxy), flush=True)
    for k, u in UP.items():
        print(tag, "up", k, curl(H + ["-H", "Content-Type: application/octet-stream", "--data-binary", "@/tmp/up.bin",
                                     "-w", "%{http_code} %{size_upload}B %{speed_upload}B/s", u], proxy), flush=True)


run("direct", False)
for host in ["speed.cloudflare.com", "cdnjs.cloudflare.com", "httpbin.org"]:
    print("cf-ray?", host, subprocess.run(["curl", "-sI", "-m", "10", "https://" + host], capture_output=True, text=True).stdout.lower().count("cf-ray"))
m = subprocess.Popen(["./mihomo", "-d", "/tmp/pb"], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
time.sleep(3)
for ip in ips:
    urllib.request.urlopen(urllib.request.Request(API + "/proxies/G", data=json.dumps({"name": ip}).encode(), method="PUT"))
    run("via " + ip, True)
m.kill()
