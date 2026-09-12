# GL Radar

Watches Nick Gross's portfolio companies across news, brand sites, Instagram,
LinkedIn and ticketing feeds. When something real happens, it scores the item,
writes a repost angle and a draft caption, pings you, and creates a ClickUp
ticket for the Gross Labs Instagram page.

Runs entirely on your machine. One SQLite file, no server to rent.

---

## Setup, about ten minutes

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in, see below
python -m radar.cli poll  # first run establishes a baseline
python -m radar.cli serve # dashboard at http://127.0.0.1:8787
```

### Keys you need

| Key | Required | Where | Cost |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | **no** | console.anthropic.com | ~$3-8/month — optional upgrade |
| `CLICKUP_TOKEN` + `CLICKUP_LIST_ID` | yes | ClickUp → Settings → Apps → API Token | free |
| `TICKETMASTER_API_KEY` | for show alerts | developer.ticketmaster.com | free, 5k calls/day |
| `IG_USER_ID` + `IG_ACCESS_TOKEN` | for Instagram | developers.facebook.com | free |
| `APIFY_TOKEN` | only for LinkedIn | apify.com | ~$30-50/month |
| `SLACK_WEBHOOK_URL` | optional | Slack app config | free |

Run `python -m radar.cli lists` to print every list in your workspace with its
id, then paste the one you want into `clickup.list_id` in
`config/settings.yaml`. To send different companies to different lists:

```yaml
clickup:
  list_id: "901234567"        # everything else
  list_overrides:
    breakaway: "901111111"    # events team
    beeup: "902222222"        # brand team
```

When you click **Create repost ticket**, the app calls the ClickUp API directly
and creates a real task in that list — with the summary, repost angle, suggested
caption, source link and a priority derived from the score. The button then
turns into a link to the task. If anything fails, the reason appears next to the
button rather than the click doing nothing.

### Instagram, the part everyone gets wrong

There is no free, durable, compliant way to scrape Instagram. Every "free IG
scraper" library breaks within weeks or gets your account rate-limited.

The official route works for you specifically, because you already run the
Gross Labs page. Instagram's Graph API has a `business_discovery` endpoint that
returns recent media for **any public Business or Creator account** as long as
the request comes from one Business account you control.

1. Convert @grosslabs to a Business account and link it to a Facebook Page.
2. Create an app at developers.facebook.com, add the Instagram product.
3. In Graph API Explorer, grant `instagram_basic`, `pages_show_list`,
   `business_management`, then exchange for a long-lived token (60 days).
4. Read your IG Business Account ID from `/me/accounts`.

Limits worth knowing: you get roughly the last 25 posts per handle, no Stories,
and it only reads Business/Creator accounts. Personal accounts fall back to
Apify if you set `APIFY_TOKEN`. Set a calendar reminder to refresh the token
every 60 days — that is the most common way this quietly stops working.

### LinkedIn

Off by default. LinkedIn has no public read API for company posts, and it is
aggressive about blocking scrapers. If you want it, set `APIFY_TOKEN` and flip
`linkedin: true` in `config/settings.yaml`. It is worth considering — the BEEUP
investment broke on Nick's own LinkedIn before it appeared anywhere else.

---

## Commands

```bash
python scripts/seed_live.py               # load hand-verified real events (run first)
python -m radar.cli poll                  # one full cycle now
python -m radar.cli poll --group news     # just news and RSS
python -m radar.cli serve                 # dashboard + background scheduler
python -m radar.cli supervise             # audit and repair the recency contract
python -m radar.cli supervise --check-only # audit without repairing, exits 1 on violation
python -m radar.cli status                # what has each collector been doing
python -m radar.cli test beeup            # dry run one company, prints scores
python -m radar.cli doctor                # what is watched, what is blind, and why
python -m radar.cli lists                 # print your ClickUp lists and their ids
python -m radar.cli export --out public   # static site for Netlify or any host
python -m radar.cli preview --out p.html  # sample-data snapshot, for screenshots
python scripts/smoke_test.py              # offline logic check, no keys needed
python scripts/live_test.py               # boots the server, clicks every button
```

`poll` and `supervise` exit non-zero on a contract violation, so cron surfaces a
degraded run instead of swallowing it. `GET /api/health` returns the same audit
as JSON with a 503 when something is wrong.

To run headless instead of keeping the dashboard open, add to crontab:

```
*/30 * * * * cd /path/to/gl-radar && .venv/bin/python -m radar.cli poll --group news
0 */2 * * *  cd /path/to/gl-radar && .venv/bin/python -m radar.cli poll --group website
0 7,19 * * * cd /path/to/gl-radar && .venv/bin/python -m radar.cli poll --group events
0 * * * *    cd /path/to/gl-radar && .venv/bin/python -m radar.cli supervise
```

---

## Staying current

**Refresh.** Top right of the dashboard. It starts a collection cycle in the
background and returns immediately, so the button never hangs for the length of
a classification pass. The header shows when the last check finished.

**Auto-refresh.** The toggle beside it, on by default, every 60 minutes. The
interval is tracked on the server rather than in each browser tab, so three open
tabs still produce one poll. Change it with `auto_refresh_minutes` in
`config/settings.yaml`.

**The daily guarantee.** A full sweep runs at 06:00 UTC (`daily_sweep_hour`),
and a supervisor check called `updated_today` fails if no cycle has completed in
24 hours — then runs one itself. Between the scheduler, auto-refresh, the daily
cron and the supervisor's catch-up, the board is never more than a day behind
unless the machine is off.

A word on that last point: everything here runs wherever you start it. On a
laptop, if the lid is shut at 07:00 the sweep does not happen — the supervisor
catches up when you next open the app. For a genuine daily guarantee it needs a
machine that stays on. **See [DEPLOY.md](DEPLOY.md)** for the options and
auto-start configs for macOS, Windows, Linux and a $5 VPS.

---

## The recency contract

Two numbers in `config/settings.yaml`, and they are treated as a contract rather
than a preference:

```yaml
freshness:
  lookback_days: 6      # nothing older is ever shown
  lookahead_days: 2     # alert on shows this close (hard ceiling: 5)
  reminder_marks: [2, 1]
```

All date arithmetic lives in `radar/freshness.py`. Nothing else is allowed to
compute its own cutoff, because that is how a second, subtly different window
gets introduced and nobody notices for a month.

The window is enforced at five separate points, on the assumption that any one
of them could be bypassed by a future change:

| Point | What it does |
|---|---|
| Collection | stale items are never stored |
| Classification | stale items are never sent to Claude, so they cost nothing |
| Display | the SQL query itself filters on `published_at` |
| Notification | a ping is a claim that something is happening now |
| Ticket creation | refuses outright with an error, even if the UI offered it |

Scheduled events are exempt from the backward window and governed by the
forward one instead — a show announced three weeks ago that happens tomorrow is
exactly what you want to hear about.

Anything that ages out while sitting in the queue is moved to **Aged out**
rather than deleted. Leaving stale items in the queue is worse than dropping
them: they look actionable, someone tickets a week-old story, and it goes up as
if it were news.

## The supervisor

Everything above is written to respect the contract. The supervisor assumes it
didn't.

It re-queries the database from outside the pipeline and checks the invariants
independently. That distinction is the whole point — a check that asks the
pipeline "did you filter correctly?" only ever confirms the pipeline's opinion
of itself. It runs after every cycle, hourly on its own timer, and on every
dashboard load.

| Check | Catches | Repairs itself |
|---|---|---|
| `config_valid` | a window edited past its ceiling | clamps and warns |
| `queue_is_fresh` | stale items visible in the queue | moves them to Aged out |
| `events_covered` | a show inside the window that was never alerted | fires the missed reminder |
| `no_stale_tickets` | out-of-window items that reached ClickUp | no — the ticket already exists |
| `updated_today` | no cycle completed in 24 hours | runs one immediately |
| `collectors_alive` | a collector that silently stopped running | no — needs a human |
| `credentials_valid` | the 60-day Instagram token expiring | no, but warns 10 days out |

Missed-reminder recovery is worded for the day it actually runs. If the two-day
reminder never fired and the supervisor only notices on the eve of the show, the
message says *tomorrow*, not *in two days*. The collector and the supervisor
share one predicate (`pending_marks`) so the two can never disagree about what
is owed.

`collectors_alive` is the check that earns its keep in practice. The realistic
failure here is not bad filtering, it is Instagram quietly returning nothing for
five weeks after a token expires while the dashboard keeps looking healthy
because news is still flowing.

---

## How it decides what matters

```
collect  ->  dedupe  ->  classify  ->  route
```

**Collect.** Six collectors, each reading `config/companies.yaml`. Google News
runs one alias-scoped query per company. Website diffing stores a link set per
watched page and reports what is new, which is how a tour date appearing on
breakawayfestival.com gets caught before anyone writes about it.

**Dedupe.** Two layers. A fingerprint on source id blocks exact repeats, and a
fuzzy title match over the last three days blocks the same story arriving from
five outlets. Without the second layer one funding announcement becomes eleven
tickets.

**Classify.** Each new item goes to Claude with that company's context paragraph
attached. It returns relevance, category, an importance score, a repost angle
and a draft caption. This is the step that knows Goldfinger the band is not
Goldfinger the Bond film, and that a Norwegian Breakaway cruise review is not
your festival. Results are cached permanently, so each item costs about a fifth
of a cent, once.

**Route.** Score 45+ enters the queue. 60+ pings you. 85+ skips review and
creates the ticket immediately. Priority-1 companies get a +15 bump so nothing
from Gross Labs itself slips past. Thresholds live in `config/settings.yaml`.

Everything else waits for a click. A wrong repost on the GL account costs more
than a late one, so the default is a human gate.

---

## config/companies.yaml is the actual product

The code is plumbing. That file is where the quality lives, and it is the only
file you should expect to edit regularly.

Each entry carries a `context` paragraph handed to the classifier verbatim, an
`exclude` list that kills the obvious collisions, and a `repost_bias` line
saying what you actually want off this brand. Adding a company is five lines:

```yaml
  - key: new_thing
    name: New Thing
    aliases: ["New Thing", "@newthing"]
    exclude: ["some famous collision"]
    context: >
      What it is, who runs it, when Gross Labs got involved, and anything
      that distinguishes it from similarly named things.
    domains: ["newthing.com"]
    instagram: ["newthing"]
    priority: 2
    repost_bias: "What makes a good GL repost from this brand."
```

Three names in the current registry are dangerously generic and already have
exclusions written: **Goldfinger** (Bond), **girlfriends** (everything), and
**Breakaway** (a Norwegian cruise ship, a Peloton class). If you add something
like "Big Noise" or "Brekky", write the exclusions on day one.

---

## Knowing whether to trust it

An empty queue and a broken system look identical from the outside. That is the
failure mode worth designing against, and it is a real one here: Los Angeles
Golf Club had no website source configured at all, so the club could publish
news on its own site and nothing would ever see it. No collector errored. No
check failed. The dashboard simply had nothing to say about that company, which
reads exactly like "nothing happened".

`python -m radar.cli doctor` resolves that ambiguity. It reports each company as:

| | meaning |
|---|---|
| `ok` | at least one real source, working |
| `thin` | Google News only — brand announcements will be missed |
| `blocked` | sources configured but unusable, normally a missing key |
| `blind` | nothing configured that could ever find anything |

The same information appears at the top of the dashboard and as a badge beside
any company in the left rail that is not fully covered, so a zero next to a name
can never be misread as silence. The supervisor's `company_coverage` check fails
on any blind or blocked company, and it does not self-repair — only a person can
decide what source to add.

Scoring itself is free by default. With no `ANTHROPIC_API_KEY`, the system falls
back to a keyword classifier (`radar/enrich/rules.py`) that uses the aliases and
exclusions already in the registry. It is noisier and writes no captions, but it
runs at zero cost and the whole project works without a paid account. Add the key
later and `mode: auto` picks it up with no other change.

## Where the starting data came from

`config/seed_events.yaml` holds events verified by hand on 12 September 2026,
each with the URL it was checked against. `python scripts/seed_live.py` loads
them so the app opens with real data before you have a Ticketmaster key. They
are stamped `hand-verified` in the signals table so you can always tell them
apart from classifier output.

That file also records what was checked and found *empty*, so a later run can
tell "no news" apart from "never looked".

Tests write to `data/test.db`, never `data/radar.db`. An earlier version shared
one file, and its invented fixture rows were mistaken for real findings. The
separation is enforced by `RADAR_DB` rather than left to discipline.

## About the preview file

`python -m radar.cli preview` writes a standalone HTML snapshot for sharing a
screenshot. It has no backend, so its tabs and filters work client-side and its
buttons are visibly disabled with a banner saying so. Do not judge the app by it
— open `http://127.0.0.1:8787` after `serve` for the real thing.

## Known limits, stated plainly

- **Instagram Stories are invisible.** No API exposes them for accounts you do
  not own. If Stories matter, someone checks manually.
- **The token expires every 60 days.** Refresh it or Instagram silently stops.
- **First run is quiet on purpose.** Website diffing needs a baseline, so page
  changes only start firing on the second poll. The supervisor will report
  collectors as "never run" until each has completed one cycle.
- **Six days is short.** That is the point, but if a slow-moving trade outlet
  covers something a week late you will not see it. Raise `lookback_days` if you
  would rather have the coverage than the tightness.
- **Ownership data drifts.** Brooklyn Pickleball, The 33rd Team and TMRW Sports
  came from third-party databases and sit at priority 3. PitchBook and Tracxn
  disagree on whether Gross Labs invested at all in 2026, so confirm before
  posting. Names drift too: OKC for Soccer became OKC United in July 2026, and
  the registry now carries both.
- **The classifier is good, not perfect.** Expect roughly one bad call in
  twenty. That is what the review queue is for.
