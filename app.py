"""小肥鸟的前哨 (outpost): the bird's public door on Render.

bot-hosting.net runs the bird but has no public URL. This small service has one, so it takes what
the bird cannot: webhooks from YouTube (new videos, pushed within seconds) and GitHub (sticker repo),
a heartbeat with an offline alert, a password-protected dashboard, and a fetch relay with a second
exit IP for pages the bot host is blocked from (B站 answers 412 there).

It is stateless on purpose. Render's free tier has no disk and wipes the container on every deploy,
crash or month-end suspension, so nothing here survives a restart and nothing has to: the bird
re-sends its snapshot and follow list with the next report, subscriptions are renewed from that, and
all configuration is environment variables (Render keeps those).

Env: OUTPOST_SECRET (shared with the bot; required), DASH_PASSWORD (dashboard; required),
DISCORD_ALERT_WEBHOOK (optional: "鸟鸟掉线了" alerts), GITHUB_WEBHOOK_SECRET (optional, recommended),
RENDER_EXTERNAL_URL (set by Render; PUBLIC_URL overrides), HUB_URL ("" disables WebSub, for tests).
"""
import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import os
import secrets
import socket
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

SECRET = os.environ.get("OUTPOST_SECRET", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
ALERT_WEBHOOK = os.environ.get("DISCORD_ALERT_WEBHOOK", "").strip()
GITHUB_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
HUB_URL = os.environ.get("HUB_URL", "https://pubsubhubbub.appspot.com/subscribe")
LEASE_SECONDS = 5 * 86400          # YouTube accepts up to 10 days; renewed when a day is left
OFFLINE_AFTER = 5 * 60             # no report for this long -> alert
FETCH_MAX_BYTES = 400_000
FETCH_TIMEOUT = 20.0
MAX_EVENTS = 300

app = FastAPI(title="小肥鸟前哨", docs_url=None, redoc_url=None)
started = time.time()
state = {
    "last_report": 0.0,            # when the bird last reported
    "snapshot": {},                # what the bird sent (tasks, boredom, trace tail, ...)
    "events": deque(maxlen=MAX_EVENTS),   # waiting for the bird to collect (each has an "id")
    "seen_videos": deque(maxlen=2000),    # YouTube re-delivers; the bird must not hear a video twice
    "subs": {},                    # youtube channel_id -> {"requested", "verified", "expires"}
    "offline_alerted": False,
    "outage_since": 0.0,
    "alerts": deque(maxlen=20),    # (time, text) shown on the dashboard
    "log": deque(maxlen=60),       # (time, text) the outpost's own notes
    "phone": {"last_seen": 0.0, "battery": {}, "jobs": deque(), "done": 0, "failed": 0},   # the home phone (phone/agent.py)
}
_phone_wake = asyncio.Event()      # set when a job is queued, so the phone's long-poll returns at once
_phone_results = {}                # job id -> {"event", "result"}
_job_seq = 0
PHONE_ONLINE_AFTER = 90            # seconds since its last poll
_event_seq = 0


def note(text):
    state["log"].append((time.time(), text))
    print(f"[outpost] {text}", flush=True)


def push_event(kind, **data):
    global _event_seq
    _event_seq += 1
    state["events"].append({"id": _event_seq, "type": kind, "at": time.time(), **data})


def require_secret(given):
    if not SECRET or not given or not hmac.compare_digest(given, SECRET):
        raise HTTPException(401, "bad secret")


def require_dash(auth):
    """HTTP Basic: any user name, DASH_PASSWORD as the password."""
    ok = False
    if DASH_PASSWORD and auth and auth.startswith("Basic "):
        try:
            _, _, pw = base64.b64decode(auth[6:]).decode("utf-8", "ignore").partition(":")
            ok = hmac.compare_digest(pw, DASH_PASSWORD)
        except Exception:
            ok = False
    if not ok:
        raise HTTPException(401, "login", headers={"WWW-Authenticate": 'Basic realm="xiaofeiniao"'})


# ---------------------------------------------------------------------------
# Bird <-> outpost
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    ago = time.time() - state["last_report"] if state["last_report"] else None
    return {"ok": True, "bot_seen_seconds_ago": None if ago is None else round(ago), "phone_online": phone_online(),
            "uptime_seconds": round(time.time() - started), "pending_events": len(state["events"])}


@app.post("/report")
async def report(request: Request, x_outpost_secret: str = Header(default="")):
    """The bird's heartbeat (once a minute). Body: {"snapshot": {...}, "youtube": [channel ids],
    "ack": [event ids]}. Returns the events waiting for it."""
    require_secret(x_outpost_secret)
    body = await request.json()
    now = time.time()
    was_offline = state["offline_alerted"]
    state["last_report"] = now
    if isinstance(body.get("snapshot"), dict):
        state["snapshot"] = body["snapshot"]
    acked = set(body.get("ack") or [])
    if acked:
        kept = [e for e in state["events"] if e["id"] not in acked]
        state["events"].clear()
        state["events"].extend(kept)
    wanted = [c for c in (body.get("youtube") or []) if isinstance(c, str) and c.startswith("UC")]
    asyncio.create_task(ensure_subscriptions(wanted))
    if was_offline:
        state["offline_alerted"] = False
        gone = now - state["outage_since"]
        await alert(f"🐦 鸟鸟回来了（掉线了 {int(gone // 60)} 分钟）")
    return {"events": list(state["events"]), "public_url": PUBLIC_URL,
            "subs": {c: s.get("verified", 0) > 0 for c, s in state["subs"].items()},
            "phone": {"online": phone_online(), "battery": state["phone"]["battery"]}}


@app.get("/fetch")
async def fetch(url: str = Query(...), accept: str = Query("text/html,application/xml,application/json,text/plain"),
                via: str = Query("auto"), x_fetch_cookie: str = Header(default=""), x_outpost_secret: str = Header(default="")):
    """Fetch a public page from here (Render's IP) and hand back the text. Private addresses are
    refused so the relay can't be pointed at anything internal. A login cookie travels in the
    X-Fetch-Cookie header, never the query string, so it stays out of the access log."""
    cookie = x_fetch_cookie
    require_secret(x_outpost_secret)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(400, "http(s) URLs only")
    if _private_host(parts.hostname):
        raise HTTPException(400, "private address refused")
    if via == "phone" or (via == "auto" and phone_online()):
        # The home phone fetches from a residential IP; "auto" uses it whenever it's there.
        if not phone_online():
            raise HTTPException(503, "phone offline")
        result = await phone_run({"kind": "fetch", "url": url, "accept": accept, "cookie": cookie}, timeout=28)
        if result is None:
            raise HTTPException(504, "phone did not answer in time")
        if result.get("error"):
            raise HTTPException(502, f"phone: {result['error']}")
        result["via"] = "phone"
        return result
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36", "Accept": accept, "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    if "bilibili.com" in parts.hostname:
        headers["Referer"] = "https://www.bilibili.com/"
    try:
        async with httpx.AsyncClient(follow_redirects=True, max_redirects=3, timeout=FETCH_TIMEOUT) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                final = str(resp.url)
                if _private_host(urlsplit(final).hostname or ""):
                    raise HTTPException(400, "redirected to a private address")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > FETCH_MAX_BYTES:
                        break
                enc = resp.charset_encoding or "utf-8"
                text = bytes(data).decode(enc, errors="ignore")
                return {"status": resp.status_code, "url": final,
                        "content_type": resp.headers.get("content-type", ""), "text": text,
                        "truncated": len(data) > FETCH_MAX_BYTES, "via": "render"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


def _private_host(host):
    host = (host or "").lower().strip("[]")
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except Exception:
            return False   # unresolvable: let the fetch fail on its own
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


# ---------------------------------------------------------------------------
# The home phone (phone/agent.py): it long-polls for jobs and posts results.
# ---------------------------------------------------------------------------
def phone_online():
    return time.time() - state["phone"]["last_seen"] < PHONE_ONLINE_AFTER


async def phone_run(job, timeout=28):
    """Queue a job for the phone and wait for its result (None on timeout)."""
    global _job_seq
    _job_seq += 1
    job = {"id": _job_seq, "at": time.time(), **job}
    slot = {"event": asyncio.Event(), "result": None}
    _phone_results[job["id"]] = slot
    state["phone"]["jobs"].append(job)
    _phone_wake.set()
    try:
        await asyncio.wait_for(slot["event"].wait(), timeout)
    except asyncio.TimeoutError:
        state["phone"]["failed"] += 1
    finally:
        _phone_results.pop(job["id"], None)
    return slot["result"]


@app.post("/phone/jobs")
async def phone_jobs(request: Request, x_outpost_secret: str = Header(default="")):
    """The phone's long-poll: {"battery": {...}, "wait": seconds}. Answers as soon as a job exists."""
    require_secret(x_outpost_secret)
    body = await request.json()
    ph = state["phone"]
    first = not phone_online()
    ph["last_seen"] = time.time()
    if isinstance(body.get("battery"), dict):
        ph["battery"] = body["battery"]
    if first:
        note("phone connected")
    wait = min(float(body.get("wait") or 20), 25)
    if not ph["jobs"]:
        _phone_wake.clear()
        try:
            await asyncio.wait_for(_phone_wake.wait(), wait)
        except asyncio.TimeoutError:
            pass
    jobs = list(ph["jobs"])
    ph["jobs"].clear()
    ph["last_seen"] = time.time()
    return {"jobs": jobs}


@app.post("/phone/result")
async def phone_result(request: Request, x_outpost_secret: str = Header(default="")):
    require_secret(x_outpost_secret)
    body = await request.json()
    slot = _phone_results.get(body.get("id"))
    state["phone"]["last_seen"] = time.time()
    if slot is None:
        return {"late": True}
    slot["result"] = body.get("result") if isinstance(body.get("result"), dict) else {"error": "bad result"}
    slot["event"].set()
    state["phone"]["done"] += 1
    return {"ok": True}


# ---------------------------------------------------------------------------
# YouTube push (WebSub via Google's hub)
# ---------------------------------------------------------------------------
def topic_for(channel_id):
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"


async def ensure_subscriptions(channel_ids):
    """Subscribe to any wanted channel that isn't verified yet or expires within a day. After a
    container wipe every subscription looks new again, which is exactly the point."""
    if not HUB_URL or not PUBLIC_URL:
        return
    now = time.time()
    for cid in channel_ids:
        sub = state["subs"].setdefault(cid, {"requested": 0.0, "verified": 0.0, "expires": 0.0})
        fresh = sub["verified"] and sub["expires"] - now > 86400
        if fresh or now - sub["requested"] < 600:
            continue
        sub["requested"] = now
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(HUB_URL, data={
                    "hub.callback": f"{PUBLIC_URL}/hub/youtube?channel={cid}", "hub.topic": topic_for(cid),
                    "hub.mode": "subscribe", "hub.verify": "async", "hub.lease_seconds": str(LEASE_SECONDS)})
            if r.status_code not in (202, 204):
                note(f"hub refused {cid}: HTTP {r.status_code} {r.text[:80]}")
        except Exception as e:
            note(f"hub request failed for {cid}: {type(e).__name__}: {e}")
    for cid in list(state["subs"]):
        if cid not in channel_ids:
            del state["subs"][cid]   # unfollowed: let the lease lapse


@app.get("/hub/youtube")
async def hub_verify(request: Request, channel: str = Query("")):
    """The hub checks that we really asked: echo its challenge."""
    q = request.query_params
    if q.get("hub.mode") not in ("subscribe", "unsubscribe") or not q.get("hub.challenge"):
        raise HTTPException(404)
    topic = q.get("hub.topic", "")
    cid = channel or topic.rsplit("channel_id=", 1)[-1]
    if q.get("hub.mode") == "subscribe":
        sub = state["subs"].setdefault(cid, {"requested": time.time(), "verified": 0.0, "expires": 0.0})
        sub["verified"] = time.time()
        sub["expires"] = time.time() + int(q.get("hub.lease_seconds") or LEASE_SECONDS)
        note(f"youtube subscription verified: {cid}")
    return PlainTextResponse(q["hub.challenge"])


@app.post("/hub/youtube")
async def hub_notify(request: Request, channel: str = Query("")):
    body = await request.body()
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return Response(status_code=204)
    for entry in root.findall("a:entry", ns):
        vid = entry.findtext("yt:videoId", default="", namespaces=ns)
        cid = entry.findtext("yt:channelId", default="", namespaces=ns) or channel
        if not vid or vid in state["seen_videos"]:
            continue
        state["seen_videos"].append(vid)
        push_event("youtube", channel_id=cid, video_id=vid, title=entry.findtext("a:title", default="", namespaces=ns)[:120],
                   link=f"https://www.youtube.com/watch?v={vid}", author=entry.findtext("a:author/a:name", default="", namespaces=ns)[:60],
                   published=entry.findtext("a:published", default="", namespaces=ns))
        note(f"youtube: new video {vid} on {cid}")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GitHub webhook (the sticker repo)
# ---------------------------------------------------------------------------
@app.post("/hook/github")
async def github_hook(request: Request, x_hub_signature_256: str = Header(default=""),
                      x_github_event: str = Header(default="")):
    body = await request.body()
    if GITHUB_SECRET:
        digest = "sha256=" + hmac.new(GITHUB_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, x_hub_signature_256):
            raise HTTPException(401, "bad signature")
    if x_github_event == "ping":
        return {"pong": True}
    if x_github_event != "push":
        return Response(status_code=204)
    data = await request.json()
    added, modified, removed = [], [], []
    for c in data.get("commits") or []:
        added += c.get("added") or []
        modified += c.get("modified") or []
        removed += c.get("removed") or []
    push_event("github", repo=(data.get("repository") or {}).get("full_name", ""), ref=data.get("ref", ""),
               added=added[:50], modified=modified[:50], removed=removed[:50],
               message=((data.get("head_commit") or {}).get("message") or "")[:200])
    note(f"github push on {(data.get('repository') or {}).get('full_name', '?')}: +{len(added)} ~{len(modified)} -{len(removed)}")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Offline alerts
# ---------------------------------------------------------------------------
async def alert(text):
    state["alerts"].append((time.time(), text))
    note(text)
    if not ALERT_WEBHOOK:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(ALERT_WEBHOOK, json={"content": text, "allowed_mentions": {"parse": []}})
    except Exception as e:
        note(f"alert webhook failed: {type(e).__name__}: {e}")


async def watchdog():
    while True:
        await asyncio.sleep(30)
        now = time.time()
        if state["last_report"] and now - state["last_report"] > OFFLINE_AFTER and not state["offline_alerted"]:
            state["offline_alerted"] = True
            state["outage_since"] = state["last_report"]
            await alert(f"⚠️ 鸟鸟掉线了：{int((now - state['last_report']) // 60)} 分钟没有心跳（{sgt(state['last_report'])} 最后一次）")
        ph = state["phone"]
        if ph["last_seen"] and not phone_online() and not ph.get("alerted"):
            ph["alerted"] = True
            await alert(f"📱 家里的手机掉线了（{sgt(ph['last_seen'])} 最后一次）")
        elif phone_online() and ph.get("alerted"):
            ph["alerted"] = False
            await alert("📱 家里的手机回来了")
        for cid, sub in list(state["subs"].items()):
            if sub["verified"] and sub["expires"] - now < 86400 and now - sub["requested"] > 600:
                await ensure_subscriptions([cid])


@app.on_event("startup")
async def _start():
    if not SECRET or not DASH_PASSWORD:
        note("OUTPOST_SECRET / DASH_PASSWORD not set: the bird and the dashboard are locked out until they are")
    note(f"outpost up at {PUBLIC_URL or '(no public url yet)'}")
    asyncio.create_task(watchdog())


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def sgt(ts):
    return time.strftime("%m-%d %H:%M", time.gmtime(ts + 8 * 3600)) if ts else "-"


def ago(ts):
    if not ts:
        return "从未"
    s = int(time.time() - ts)
    return f"{s}秒前" if s < 60 else f"{s // 60}分钟前" if s < 3600 else f"{s // 3600}小时前"


@app.get("/", response_class=HTMLResponse)
async def dashboard(authorization: str = Header(default="")):
    require_dash(authorization)
    snap = state["snapshot"]
    e = html.escape
    online = state["last_report"] and time.time() - state["last_report"] < OFFLINE_AFTER
    rows = lambda items: "".join(f"<li>{e(str(i))}</li>" for i in items) or "<li class=dim>（无）</li>"
    bars = "".join(f"<div class=bar><span>{e(str(c.get('name', c.get('id'))))}</span><i style='width:{int(float(c.get('level', 0)) * 100)}%'></i>"
                   f"<b>{int(float(c.get('level', 0)) * 100)}%</b> <small>{e(str(c.get('last', '')))}</small></div>"
                   for c in snap.get("boredom") or []) or "<p class=dim>（无）</p>"
    tasks = "".join(f"<tr><td>{e(str(t.get('by', '')))}</td><td>{e(str(t.get('what', '')))}</td><td>{e(str(t.get('due', '')))}</td><td>{e(str(t.get('where', '')))}</td></tr>"
                    for t in snap.get("tasks") or []) or "<tr><td colspan=4 class=dim>没有待办</td></tr>"
    subs = "".join(f"<li>{e(c)}：{'已订阅' if s.get('verified') else '等待验证'}（到 {sgt(s.get('expires'))}）</li>" for c, s in state["subs"].items()) or "<li class=dim>（无）</li>"
    events = "".join(f"<li>#{ev['id']} {e(ev['type'])} {e(str({k: v for k, v in ev.items() if k in ('title', 'repo', 'message')}))}</li>" for ev in list(state["events"])[-20:]) or "<li class=dim>（无）</li>"
    notes = "".join(f"<li><small>{sgt(t)}</small> {e(x)}</li>" for t, x in list(state["log"])[-25:][::-1])
    alerts = "".join(f"<li><small>{sgt(t)}</small> {e(x)}</li>" for t, x in list(state["alerts"])[::-1]) or "<li class=dim>（无）</li>"
    trace = e("\n".join(snap.get("trace") or [])) or "（鸟鸟还没发来日志）"
    page = f"""<!doctype html><html lang=zh><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>小肥鸟前哨</title><meta http-equiv=refresh content=60>
<style>
:root{{--bg:#f7f5f0;--card:#fff;--ink:#222;--dim:#888;--ok:#2e9e5b;--bad:#d9534f;--bar:#f3b74a}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181d;--card:#1f2228;--ink:#e8e6e1;--dim:#8a8f99}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}}
h1{{font-size:20px;margin:0 0 12px}} h2{{font-size:15px;margin:0 0 8px;color:var(--dim);font-weight:600}}
.grid{{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}}
.card{{background:var(--card);border-radius:12px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.status{{display:inline-block;padding:2px 10px;border-radius:999px;color:#fff;background:{'var(--ok)' if online else 'var(--bad)'}}}
.dim{{color:var(--dim)}} ul{{margin:0;padding-left:18px}} li{{margin:2px 0}} table{{width:100%;border-collapse:collapse}} td{{padding:4px 6px;border-top:1px solid rgba(128,128,128,.2);vertical-align:top}}
.bar{{display:grid;grid-template-columns:120px 1fr 44px auto;gap:8px;align-items:center;margin:4px 0}} .bar i{{display:block;height:10px;background:var(--bar);border-radius:5px;min-width:2px}}
pre{{white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:420px;overflow:auto;background:rgba(128,128,128,.08);padding:8px;border-radius:8px;margin:0}}
.kv span{{display:inline-block;margin-right:14px}}
</style></head><body>
<h1>🐦 小肥鸟前哨 <span class=status>{'在线' if online else '掉线'}</span> <small class=dim>最后心跳 {ago(state['last_report'])}</small></h1>
<div class=grid>
<div class=card><h2>今天</h2><div class=kv><span>脑子：{e(str(snap.get('provider', '?')))}</span><span>今日 token：{e(str(snap.get('tokens_today', '?')))}</span><span>鸟鸟开机：{e(str(snap.get('uptime', '?')))}</span><span>版本：{e(str(snap.get('version', '?')))}</span></div>
<pre>{e(str(snap.get('day_report') or '（还没干啥）'))}</pre></div>
<div class=card><h2>待办（{len(snap.get('tasks') or [])}）</h2><table><tr class=dim><td>谁</td><td>什么</td><td>何时</td><td>哪里</td></tr>{tasks}</table></div>
<div class=card><h2>无聊条</h2>{bars}</div>
<div class=card><h2>关注</h2><ul>{rows(snap.get('follows') or [])}</ul><h2 style="margin-top:10px">YouTube 推送订阅</h2><ul>{subs}</ul></div>
<div class=card><h2>待鸟鸟领取的事件（{len(state['events'])}）</h2><ul>{events}</ul><h2 style="margin-top:10px">掉线记录</h2><ul>{alerts}</ul></div>
<div class=card><h2>家里的手机</h2><p>{'<span class=status>在线</span>' if phone_online() else '<span class=status style="background:var(--bad)">掉线</span>'} 最后一次 {ago(state['phone']['last_seen'])}，电量 {e(str(state['phone']['battery'].get('percent', '?')))}%（{e(str(state['phone']['battery'].get('status', '')))}），做过 {state['phone']['done']} 件事，超时 {state['phone']['failed']} 次</p></div>
<div class=card><h2>前哨日志</h2><ul>{notes or '<li class=dim>（无）</li>'}</ul><p class=dim>前哨开机 {ago(started)}，公网地址 {e(PUBLIC_URL or '未知')}</p></div>
</div>
<div class=card style="margin-top:12px"><h2>鸟鸟最近的日志（最后 {len(snap.get('trace') or [])} 行）</h2><pre>{trace}</pre></div>
</body></html>"""
    return HTMLResponse(page)
