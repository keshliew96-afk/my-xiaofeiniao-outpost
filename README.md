# 小肥鸟前哨 (outpost)

The bird lives on bot-hosting.net, which has no public URL. This small FastAPI service runs on
Render's free tier and is her public door:

- **YouTube push**: new videos from `!关注` channels arrive within seconds (WebSub), not on the polling timer.
- **GitHub webhook**: a push to the sticker repo makes her fetch the new stickers right away.
- **Offline alert**: no heartbeat for 5 minutes posts "鸟鸟掉线了" to a Discord webhook, and "回来了" when she's back.
- **Dashboard** (`/`, password): tasks, boredom bars, today's doings, follows, token spend, last 150 trace lines.
- **Fetch relay** (`/fetch`): pages fetched from Render's IP for sites that block the bot host (B站 412).

It is **stateless by design**. Render's free tier has no disk and wipes the container on deploy, crash
or month-end suspension. Nothing here needs to survive that: config is env vars (Render keeps them),
the bird re-sends everything with her next report, and subscriptions are renewed from that.

## Deploy (once)

1. **Render → New → Web Service**, connect this repo. Runtime Python, build `pip install -r requirements.txt`,
   start `uvicorn app:app --host 0.0.0.0 --port $PORT`, plan **Free**. (Or use the Blueprint: `render.yaml`.)
2. **Environment variables** on the service:
   | name | value |
   |---|---|
   | `OUTPOST_SECRET` | a long random string (e.g. 40 letters); the same goes into the bot's `.env` |
   | `DASH_PASSWORD` | the dashboard password |
   | `DISCORD_ALERT_WEBHOOK` | optional: Discord channel → Edit → Integrations → Webhooks → New → copy URL |
   | `GITHUB_WEBHOOK_SECRET` | optional: any random string, repeated in the GitHub webhook below |
3. Deploy. Note the service URL, e.g. `https://xiaofeiniao-outpost.onrender.com`. Open it, log in with
   any user name and `DASH_PASSWORD`.
4. **Bot side** (bot-hosting `.env`): `OUTPOST_URL=https://...onrender.com` and `OUTPOST_SECRET=...`, then restart
   the bot. The dashboard shows 在线 within a minute. The bird's minute report is also the keep-alive,
   so the service never sleeps while she's up.
5. **Sticker repo webhook** (optional): GitHub repo → Settings → Webhooks → Add. Payload URL
   `https://...onrender.com/hook/github`, content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`,
   event: just the push event.
6. YouTube subscriptions need nothing from you: the bird sends her followed channel ids, the outpost
   subscribes and renews. The dashboard lists them with 已订阅 once Google verified them.

## Free-tier rules

- 750 instance-hours a month per account, shared by all free web services. One service kept awake
  all month uses ~720, so **this must be the only awake service on the account**.
- No disk. Never configure anything inside the container by hand; it will be gone.
- A cold start after a wipe takes ~1 minute; the bird's next report refills the dashboard.

## Endpoints

| path | who | what |
|---|---|---|
| `GET /health` | anyone | liveness; seconds since the bird's last report |
| `POST /report` | bird (secret header) | heartbeat + snapshot + followed channels + acks → pending events |
| `GET /fetch?url=` | bird (secret header) | fetch a public page from Render's IP; cookie via `X-Fetch-Cookie` |
| `GET/POST /hub/youtube` | Google's hub | WebSub verification / new-video notifications |
| `POST /hook/github` | GitHub | push events (HMAC-checked when a secret is set) |
| `GET /` | you (Basic auth) | dashboard |
