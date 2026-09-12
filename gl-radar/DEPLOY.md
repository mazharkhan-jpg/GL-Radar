# Running it so it actually updates daily

Short answer: **on a laptop it updates when the laptop is awake, not daily.**
The scheduler lives inside the `serve` process. Close the lid and nothing
polls; there is no daemon on your machine that wakes it, and no cloud service
running in the background.

What saves this in practice is the supervisor. When the machine comes back, the
`updated_today` check notices no cycle has completed in 24 hours and runs one
immediately. So if you open your laptop every morning, you get a fresh queue a
minute or two later. That is genuinely fine for most weeks. It is not fine the
week you are travelling, or on the morning of a show.

---

## Cost

Everything here is free: Netlify hosting, GitHub Actions, Google News, RSS,
website checks, a Ticketmaster key, and ClickUp. Scoring defaults to a free
keyword classifier, so no paid API account is needed.

The only optional spend is `ANTHROPIC_API_KEY` at roughly $3-8/month, which buys
better filtering and written captions. Start without it.

## About Netlify specifically

Netlify drag-and-drop hosts **static files only** — HTML, CSS, JavaScript. It has
no Python runtime that can hold a process open, no persistent disk for the
SQLite database, and no way to run a scheduler. Dropping this project there
publishes nothing useful.

Netlify Functions do run code, but they are serverless: they start, run for
seconds, and die with no disk. A collection cycle takes minutes and the dedup
history has to survive between runs. Porting to that shape means a rewrite plus
a hosted database, which is a lot of work to end up with less than you have.

What *does* work is splitting the job in two, which is Option D below.

## The four options

| | Local laptop | Always-on box | Actions + Netlify |
|---|---|---|---|
| Cost | free | ~$5/month | free |
| Truly daily | only if the laptop is on | yes | yes |
| Live URL on your phone | no | no | **yes** |
| Dashboard | full | full, over SSH tunnel | read-only |
| Approve or dismiss in the UI | yes | yes | no — tickets auto-create |
| Alerts | desktop | Slack | Slack |
| Setup | 5 min | 20 min | 25 min, once |
| Right for you if | you are trying it out | you want the review queue | **you want it live and free** |

### Option A — Local, with auto-start

Cheapest and closest to where you are now. The configs in `deploy/` restart the
app after a reboot so you are not remembering to launch it.

- **macOS** — edit the paths in `deploy/com.glradar.agent.plist`, then:
  ```bash
  cp deploy/com.glradar.agent.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.glradar.agent.plist
  ```
- **Windows** — edit `$AppDir` in `deploy/windows-task.ps1`, run it once as
  Administrator.
- **Linux desktop** — use the systemd unit below with your own user.

This does not keep the machine awake, and you should not make it. A laptop
pinned awake overnight to poll a festival lineup is a bad trade.

### Option B — A small always-on machine

This is the version that does what you asked for. Any of these work:

- A $5–6/month VPS (Hetzner CX22, DigitalOcean, Vultr). Cheapest reliable path.
- A Raspberry Pi on your desk. No monthly cost, same result.
- An old laptop left on with the lid-close action set to "do nothing".

For a VPS:

```bash
scp -r gl-radar root@YOUR_SERVER:/tmp/
ssh root@YOUR_SERVER 'bash /tmp/gl-radar/deploy/install-vps.sh'
ssh root@YOUR_SERVER 'nano /opt/gl-radar/.env'    # add your keys
ssh root@YOUR_SERVER 'systemctl start gl-radar'
```

Two changes worth making on a server, since there is no desktop to notify:

```yaml
# config/settings.yaml
notify:
  desktop: false
  slack_webhook: "${SLACK_WEBHOOK_URL}"
```

To see the dashboard without putting it on the public internet:

```bash
ssh -L 8787:127.0.0.1:8787 root@YOUR_SERVER
# then open http://127.0.0.1:8787 locally
```

**Do not change the bind address to 0.0.0.0.** The dashboard has no login. It
holds your review queue and can spend your ClickUp token. Keep it on localhost
and tunnel in, or put Tailscale on the box and reach it over the tailnet.

### Option C — GitHub Actions, no server

Free and genuinely daily, at the cost of the dashboard. A scheduled workflow
runs `radar poll`, tickets go straight to ClickUp, and you review in ClickUp
instead of the queue. The catch is state: SQLite has to be committed back to the
repo each run or you lose dedup history and every cycle re-reports the same
stories. It works, it is a little ugly, and it is the right answer only if you
were going to live in ClickUp anyway.

### Option D — GitHub Actions does the work, Netlify shows the result

This is the free, always-live answer, and it is what you asked for once the
Netlify part is understood correctly. The work happens on GitHub's machines; the
page Netlify serves is the output.

```
01:30 UTC daily   GitHub Actions runs the real collectors
                  -> scores everything with Claude
                  -> creates ClickUp tickets above the threshold
                  -> rebuilds public/index.html
                  -> pushes it to Netlify
                  -> commits the database back so dedup survives
```

You get a `something.netlify.app` URL that works on your phone, updates every
morning before you start, and costs nothing. The trade is that the page is
read-only: there are no approve and dismiss buttons, because there is no server
to click against. Each item gives you **Copy caption** and **Open source**, and
tickets arrive in ClickUp on their own.

Practically, that means the review step moves from the dashboard into ClickUp.
Given that you were creating a repost ticket for everything you approved anyway,
this is a smaller loss than it sounds — the queue and the ticket board collapse
into one place.

**Setup**

1. Push this project to GitHub as a **private** repo. It contains your review
   queue and, once it runs, real data. Do not make it public.
2. `python -m radar.cli export` locally, then drag the resulting `public/`
   folder onto `app.netlify.com/drop`. That creates the site and gives you the
   URL immediately.
3. In Netlify, copy the **Site ID** from Site configuration > General, and make
   a token at `app.netlify.com/user/applications`.
4. On GitHub: Settings > Secrets and variables > Actions, add `ANTHROPIC_API_KEY`,
   `CLICKUP_TOKEN`, `CLICKUP_LIST_ID`, `TICKETMASTER_API_KEY`, `NETLIFY_AUTH_TOKEN`,
   `NETLIFY_SITE_ID`, and the Instagram pair when you have them.
5. Actions tab > "GL Radar daily" > Run workflow. It publishes in about a minute.

The workflow is `.github/workflows/daily-radar.yml`. Running it manually is your
refresh button when you want an update mid-day.

**Two things to know.** The published page carries `noindex` headers and a
`robots.txt` that disallows everything, because the queue can name deals before
they are announced — but a Netlify URL is still public to anyone who has it, so
treat it as unlisted rather than private. And if you later want auto-ticketing to
be less eager, raise `auto_ticket_at` in `config/settings.yaml`; on this setup it
is the only thing creating tickets.

---

## What I would actually do

**Option D.** It is free, it is genuinely daily, and it gives you the live URL
you were after. Netlify is part of it, just not the part doing the work.

Run **Option A** alongside it on your laptop whenever you want the full review
queue with approve and dismiss. The two share nothing and cannot conflict; the
laptop copy keeps its own database.

Move to **Option B** only if you find yourself wanting to approve things from the
dashboard rather than ClickUp, or if the auto-ticketing turns out to be too
noisy and you want a human gate back. That is a real possibility and you should
not pre-solve it — see how a week of tickets feels first.

Skip Option C; Option D is the better version of the same idea.

---

## Either way, do these two things

**Set the sweep hour to something useful.** `daily_sweep_hour` in
`config/settings.yaml` now runs in your timezone (`Asia/Kolkata`), currently
07:00 — the queue is ready before you start work. On a laptop this only fires if
the machine is on at 07:00; the supervisor catches up otherwise.

**Put a reminder in your calendar for the Instagram token, every 55 days.** It
expires after 60. This is the single most likely way the whole thing quietly
stops working while continuing to look healthy, because news keeps flowing from
Google News and only the Instagram collector goes dark. Set `IG_TOKEN_ISSUED` in
`.env` and the supervisor will warn you ten days out — but only if something is
running to warn you.

## Checking it is alive

```bash
python -m radar.cli status        # last run per collector, last full check
python -m radar.cli supervise     # audit, exits 1 if anything is wrong
curl -s localhost:8787/api/health # same thing as JSON, 503 when unhealthy
```

On a server, point any uptime monitor at `/api/health` through the tunnel and
you will hear about a dead collector instead of discovering it in a month.
