# Deploying Draft Copilot (ffbot)

Runbook for the hosted service. The app is a stdlib-only Python 3.10+ server
started with:

```
python -m ffbot.cli serve --host 0.0.0.0 --port 8080 --public
```

The Docker image (see `Dockerfile`) bakes in `ffbot/`, `data/guide_2026.json`,
and `data/rookies_2026.json`. The SQLite db is created inside the container at
`data/ffbot.db` and is **ephemeral** - a redeploy or restart wipes sessions.
That is acceptable for v1; do not promise persistence to users.

> **Flag prerequisite:** `--host` and `--public` are being added in the main
> workstream. Until they land, the server refuses to bind anything but
> loopback and this image will not serve traffic. Deploy only after that
> change ships.

---

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `FFBOT_PUBLIC` | Yes (`1`) | Runs the server in public/hosted mode. Set in the image and in both platform configs. |
| `ANTHROPIC_API_KEY` | No | Enables the LLM-backed chat. **OFF by default** for the public deployment - unset means chat falls back to the non-LLM parser. Turn it on only once you have thought about abuse and cost, ideally with rate limiting in place. |
| `YAHOO_CLIENT_ID` / `YAHOO_CLIENT_SECRET` | No | Yahoo Fantasy beta via official OAuth. Leave unset to run Sleeper-only. |
| `FFBOT_SESSION_TTL` | No | Session lifetime in seconds. Leave unset for the default. |

Set secrets through the platform's secret store, never in the repo:

- Fly: `fly secrets set ANTHROPIC_API_KEY=...`
- Render: dashboard > service > Environment (the blueprint marks them `sync: false` so Render prompts for them).

---

## Option A: Fly.io

1. Install `flyctl` and `fly auth login`.
2. Edit `fly.toml`: replace `ffbot-CHANGE-ME` with a real app name and pick a
   `primary_region`.
3. From the repo root:

   ```
   fly launch --no-deploy   # reuses the existing fly.toml and Dockerfile; say no to Postgres/Redis
   fly deploy
   ```

4. Pin it to a single machine - v1 state is in-process SQLite, so two
   machines means two disagreeing draft boards:

   ```
   fly scale count 1
   ```

5. Optional secrets:

   ```
   fly secrets set YAHOO_CLIENT_ID=... YAHOO_CLIENT_SECRET=...
   ```

6. Custom domain: `fly certs add YOUR_DOMAIN`, then create the DNS records it
   tells you to (see DNS below). HTTPS is enforced by `force_https` in
   `fly.toml`; the health check polls `GET /api/ping`.

## Option B: Render

1. Push the repo to GitHub/GitLab.
2. Render dashboard > **New > Blueprint**, point it at the repo. It reads
   `render.yaml` and builds from the `Dockerfile`.
3. When prompted for the `sync: false` env vars, set only what you need
   (see the table above) - all of them can stay empty for a Sleeper-only,
   LLM-off deployment.
4. Do not downgrade to the free plan for real use: free instances spin down
   on idle, which kills in-progress draft sessions and adds cold-start
   latency exactly when a draft clock is running.
5. Custom domain: service > Settings > Custom Domains > add `YOUR_DOMAIN`,
   then create the CNAME it shows you. Render provisions TLS automatically.

---

## DNS

- Apex domain (`YOUR_DOMAIN`): use the A/AAAA records Fly prints from
  `fly certs add`, or Render's apex instructions (ALIAS/ANAME or their
  fallback A record, depending on your DNS host).
- Subdomain (`app.YOUR_DOMAIN`): a CNAME to the platform hostname
  (`<app>.fly.dev` or `<service>.onrender.com`).
- Wait for certificate issuance to show verified before announcing the URL.

## Chrome extension update after deploy

The extension ships pointed at a placeholder backend. After the service is
live:

1. Set the extension's API base URL to `https://YOUR_DOMAIN` (no trailing
   slash) in the extension config.
2. Make sure the host permission in `manifest.json` covers
   `https://YOUR_DOMAIN/*`.
3. Repackage and upload to the Chrome Web Store listing (`EXTENSION_ID`);
   existing installs pick up the change on the next extension update, not
   instantly - expect the rollout to take hours to days.
4. Sanity-check one real browser: open a Sleeper draft, confirm the docked
   panel talks to the hosted API and not localhost.

## Smoke test checklist

Run all of these before telling anyone the service is up:

- [ ] `curl -fsS https://YOUR_DOMAIN/api/ping` returns 200.
- [ ] `curl -fsS -o /dev/null -w '%{redirect_url}\n' http://YOUR_DOMAIN/api/ping`
      redirects to https.
- [ ] Connect a Sleeper **mock draft** end to end: create a mock on Sleeper,
      connect the panel, confirm picks mirror within a few seconds and
      recommendations update.
- [ ] Chat answers a plain-English question. With `ANTHROPIC_API_KEY` unset,
      confirm it degrades to the built-in parser instead of erroring.
- [ ] Watch memory across one full mock draft (`fly status` /
      `fly machine status`, or Render's Metrics tab). A shared-cpu-1x /
      starter instance is small; if memory climbs steadily, capture it now,
      not during a real draft night.
- [ ] Restart the instance and confirm the app comes back healthy (accepting
      that in-flight sessions are lost - that is the v1 tradeoff).
- [ ] Check platform logs for tracebacks after the mock (`fly logs`, or
      Render > Logs).

## Costs (honest ranges)

Prices move; check fly.io/pricing and render.com/pricing before budgeting.
As of mid-2026, rough shape:

- **Fly.io**: a single shared-cpu-1x machine with 512 MB is on the order of
  a few dollars a month (roughly $3-6), plus small bandwidth charges.
  Fly bills usage; an always-on machine is the dominant line item.
- **Render**: Starter web service is a fixed monthly fee (roughly $7/mo
  historically). Free tier exists but spins down - not suitable here.
- **Domain**: typically $10-20/year depending on TLD and registrar.
- **Anthropic API** (only if you enable chat): entirely usage-dependent.
  Draft-night chat is bursty; there is no honest flat estimate. Start with
  it off, or on with a hard monthly spend cap on the API key.

No other required costs. Sleeper's public API is free; Yahoo OAuth is free
to register.

Questions or problems: CONTACT_EMAIL.

## Vercel (landing page only)

Vercel hosts the static site (`site/`) beautifully, but it cannot run the
engine: the service keeps live sessions, an SQLite store and multi-second
Monte Carlo runs in one long-lived process, and Vercel functions are
stateless with short execution limits. The working split:

1. Deploy the engine to Fly or Render (sections above) - that is the app.
2. Import this repo into Vercel; `vercel.json` serves `site/` with no build
   step. Replace both `YOUR_APP_HOST` rewrites in `vercel.json` with your
   Fly/Render hostname so `/app` and `/api/*` on the Vercel domain proxy to
   the real engine.
3. Point your domain at Vercel; the landing, /privacy and /terms live there.
