#!/usr/bin/env python3
"""Create or update one TXT record on Cloudflare DNS with the contents of a proxyip txt file.

Environment:
  CF_API_TOKEN  required  token with Zone:DNS:Edit on the one zone only
  CF_ZONE       required  e.g. yieti.top
  RECORD        required  full name, e.g. us.proxyip.yieti.top
  FILE          default proxyip/us.txt
  CF_API        default https://api.cloudflare.com/client/v4 (override for tests)
"""
import json
import os
import sys
import urllib.parse
import urllib.request

API = os.environ.get("CF_API", "https://api.cloudflare.com/client/v4")
TOKEN = os.environ["CF_API_TOKEN"]
ZONE = os.environ["CF_ZONE"]
RECORD = os.environ["RECORD"]
FILE = os.environ.get("FILE", "proxyip/us.txt")


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")
    if not r.get("success"):
        sys.exit(f"{method} {path}: {r.get('errors')}")
    return r["result"]


def main():
    value = open(FILE).read().strip()
    ips = [x for x in value.split(",") if x]
    if len(ips) < 2:
        sys.exit(f"{FILE} has {len(ips)} entries; refusing to publish")
    if not RECORD.endswith("." + ZONE):
        sys.exit(f"{RECORD} is not inside zone {ZONE}")
    content = '"' + ",".join(ips) + '"'

    zid = os.environ.get("CF_ZONE_ID")
    if not zid:
        zones = call("GET", "/zones?" + urllib.parse.urlencode({"name": ZONE}))
        if len(zones) != 1:
            sys.exit(f"zone {ZONE} not found (or token cannot see it); set CF_ZONE_ID")
        zid = zones[0]["id"]
    recs = call("GET", f"/zones/{zid}/dns_records?" + urllib.parse.urlencode({"type": "TXT", "name": RECORD}))
    body = {"type": "TXT", "name": RECORD, "content": content, "ttl": 300,
            "comment": "managed by edgetunnel-proxyip GitHub Action"}
    if not recs:
        call("POST", f"/zones/{zid}/dns_records", body)
        print("created", RECORD, content)
    elif recs[0]["content"] == content:
        print("unchanged", RECORD)
    else:
        call("PUT", f"/zones/{zid}/dns_records/{recs[0]['id']}", body)
        print("updated", RECORD, recs[0]["content"], "->", content)


if __name__ == "__main__":
    main()
