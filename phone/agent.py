"""The bird's home limb: runs in Termux on a spare phone on the owner's home Wi-Fi.

The phone has no public address, so it dials out: it long-polls the outpost (Render) for jobs,
does them from the home connection (a residential IP that B站 and friends accept, unlike the
datacentre IPs of bot-hosting and Render), and posts the results back. Standard library only, so
it runs on a bare Termux python.

Jobs today: "fetch" (GET a URL with browser headers, optional cookie, return text) and "ping".
Config: ~/.outpost-agent.env with OUTPOST_URL and OUTPOST_SECRET (same secret as the bird).
Start: python agent.py   (Termux:Boot runs it at boot via ~/.termux/boot/start.sh)
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_FILE = os.path.expanduser("~/.outpost-agent.env")
UA = ("Mozilla/5.0 (Linux; Android 14; V2332) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Mobile Safari/537.36")
FETCH_MAX_BYTES = 400_000
LOG = os.path.expanduser("~/agent.log")


def load_env():
    env = {}
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    env.setdefault("OUTPOST_URL", os.environ.get("OUTPOST_URL", ""))
    env.setdefault("OUTPOST_SECRET", os.environ.get("OUTPOST_SECRET", ""))
    return env


ENV = load_env()
BASE = ENV["OUTPOST_URL"].rstrip("/")
HEADERS = {"X-Outpost-Secret": ENV["OUTPOST_SECRET"], "Content-Type": "application/json"}


def log(text):
    line = time.strftime("%m-%d %H:%M:%S ") + text
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if os.path.getsize(LOG) > 500_000:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def call(path, body=None, timeout=40):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def battery():
    """termux-battery-status when Termux:API is installed; {} otherwise."""
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=10).stdout
        b = json.loads(out)
        return {"percent": b.get("percentage"), "status": b.get("status"), "temp": b.get("temperature")}
    except Exception:
        return {}


def decompress(data, encoding):
    """urllib hands back the raw body; some servers (B站's comment server) deflate it without being asked."""
    import gzip
    import zlib
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc or data[:2] == b"\x1f\x8b":
            return gzip.decompress(data)
        if "deflate" in enc:
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -15)
        if "br" in enc:
            return data   # brotli isn't in the standard library; we never ask for it
    except Exception:
        pass
    return data


def do_fetch(job):
    url = job.get("url", "")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"error": "http(s) URLs only"}
    headers = {"User-Agent": UA, "Accept": job.get("accept") or "text/html,application/xml,application/json,text/plain",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    if job.get("cookie"):
        headers["Cookie"] = job["cookie"]
    if "bilibili.com" in parts.hostname:
        headers["Referer"] = "https://www.bilibili.com/"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read(4 * FETCH_MAX_BYTES)
            data = decompress(data, resp.headers.get("content-encoding", ""))
            ctype = resp.headers.get("content-type", "")
            charset = resp.headers.get_content_charset() or "utf-8"
            return {"status": resp.status, "url": resp.geturl(), "content_type": ctype,
                    "text": data[:FETCH_MAX_BYTES].decode(charset, "ignore"), "truncated": len(data) > FETCH_MAX_BYTES}
    except urllib.error.HTTPError as e:
        body = e.read(FETCH_MAX_BYTES) if e.fp else b""
        return {"status": e.code, "url": url, "content_type": e.headers.get("content-type", "") if e.headers else "",
                "text": body.decode("utf-8", "ignore"), "truncated": False}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def run_job(job):
    kind = job.get("kind")
    if kind == "fetch":
        return do_fetch(job)
    if kind == "ping":
        return {"pong": True, "host": socket.gethostname()}
    return {"error": f"unknown job kind {kind!r}"}


def main():
    if not BASE or not ENV["OUTPOST_SECRET"]:
        print(f"set OUTPOST_URL and OUTPOST_SECRET in {ENV_FILE}", file=sys.stderr)
        sys.exit(1)
    log(f"agent up, outpost {BASE}")
    backoff = 5
    while True:
        try:
            answer = call("/phone/jobs", {"battery": battery(), "wait": 25}, timeout=40)
            backoff = 5
        except Exception as e:
            log(f"poll failed: {type(e).__name__}: {e}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue
        for job in answer.get("jobs") or []:
            started = time.time()
            result = run_job(job)
            try:
                call("/phone/result", {"id": job.get("id"), "result": result, "took": round(time.time() - started, 2)})
            except Exception as e:
                log(f"result post failed for {job.get('id')}: {type(e).__name__}: {e}")
            log(f"job {job.get('id')} {job.get('kind')} -> {result.get('status') or result.get('error') or 'ok'}")


if __name__ == "__main__":
    main()
