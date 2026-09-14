"""Local review queue at http://127.0.0.1:8787

Nothing reaches ClickUp without a person clicking, unless it scored above
auto_ticket_at. A wrong repost on the GL account costs more than a missed one.

Two rendering modes:
  live   served by this app, buttons hit the API, links are real routes
  demo   a standalone HTML file with no backend, for looking at the layout.
         Tabs and filters work client-side; actions are visibly disabled
         rather than silently dead.
"""
from __future__ import annotations

import html
import json
import logging
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import db
from .pipeline import Pipeline

log = logging.getLogger("radar.server")
app = FastAPI(title="GL Radar")
pipe = Pipeline()

# Guards against two overlapping collection cycles, which would double-charge
# the classifier and race on cursor writes.
_poll_lock = threading.Lock()
_poll_state: dict = {"running": False, "started_at": "", "result": None, "error": ""}

# Two views, not five. Ticketed and Passed-on were states of a review workflow
# that does not exist on a static page, so they were labels with nothing behind
# them. What is left is the honest split: things to act on, and things to
# look back at.
TABS = [("pending", "To review"), ("archive", "Archive")]

STYLE = """
  :root {
    --paper:#F2F4F3; --card:#FFFFFF; --ink:#141A1C; --soft:#5C6A6E;
    --rule:#DDE3E1; --signal:#E03E2F; --done:#2F6F63; --amber:#E8A317;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font-family:Archivo,system-ui,sans-serif; font-size:15px; line-height:1.5; }
  a { color:inherit; }
  header { display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
           padding:1.5rem 2rem 1.1rem; border-bottom:2px solid var(--ink); }
  h1 { font-size:1.5rem; font-weight:800; letter-spacing:-.03em; margin:0; }
  .tally { color:var(--soft); font-size:.85rem; }
  .tally b { color:var(--ink); font-weight:600; }
  .controls { margin-left:auto; display:flex; align-items:center; gap:.75rem; }
  .checked { color:var(--soft); font-size:.78rem; font-variant-numeric:tabular-nums; }
  .auto { display:flex; align-items:center; gap:.35rem; color:var(--soft); font-size:.78rem; }
  .auto input { accent-color:var(--ink); }
  .wrap { display:grid; grid-template-columns:210px 1fr; gap:2rem;
          padding:1.5rem 2rem 4rem; max-width:1180px; }
  nav { position:sticky; top:1.5rem; align-self:start; }
  nav a { display:flex; justify-content:space-between; gap:.5rem; padding:.4rem .6rem;
          border-radius:4px; text-decoration:none; color:var(--soft); font-size:.875rem; }
  nav a:hover { background:#E6EAE9; color:var(--ink); }
  nav a[aria-current] { background:var(--ink); color:var(--paper); font-weight:600; }
  .sector { display:flex; justify-content:space-between; align-items:baseline;
            margin:1.15rem 0 .3rem; padding:0 .6rem .3rem;
            border-bottom:1px solid var(--rule);
            font-size:.68rem; font-weight:700; letter-spacing:.09em;
            text-transform:uppercase; color:var(--soft); }
  .sector:first-of-type { margin-top:.9rem; }
  .sector span:last-child { font-weight:600; letter-spacing:0; }
  nav > a:first-child { margin-bottom:.2rem; }
  .tabs { display:flex; gap:1.25rem; margin-bottom:1.25rem; border-bottom:1px solid var(--rule); }
  .tabs a { padding:.5rem 0 .6rem; text-decoration:none; color:var(--soft); font-size:.9rem; }
  .tabs a[aria-current] { color:var(--ink); font-weight:600; box-shadow:inset 0 -2px 0 var(--signal); }
  article { background:var(--card); border:1px solid var(--rule); border-radius:6px;
            padding:1rem 1.15rem; margin-bottom:.65rem; }
  .top { display:flex; align-items:center; gap:.75rem; margin-bottom:.5rem; }
  .co { font-weight:600; font-size:.8rem; }
  .meta { color:var(--soft); font-size:.78rem; margin-left:auto; white-space:nowrap; }
  .meter { display:flex; align-items:center; gap:.5rem; }
  .track { width:104px; height:5px; background:#E6EAE9; border-radius:3px; overflow:hidden; }
  .fill { height:100%; background:var(--amber); }
  .fill.hot { background:var(--signal); }
  .score { font-size:.72rem; color:var(--soft); font-variant-numeric:tabular-nums; min-width:1.6rem; }
  h2 { font-size:1rem; font-weight:600; margin:0 0 .35rem; letter-spacing:-.01em; }
  h2 a { text-decoration:none; }
  h2 a:hover { box-shadow:inset 0 -1px 0 var(--ink); }
  .sum { color:var(--soft); font-size:.9rem; margin:0 0 .75rem; max-width:68ch; }
  details summary { cursor:pointer; font-size:.82rem; color:var(--soft); margin-bottom:.6rem; }
  details summary::marker { color:var(--rule); }
  .cap { background:var(--paper); border-left:2px solid var(--signal);
         padding:.6rem .8rem; margin:.5rem 0; font-size:.9rem; max-width:64ch; }
  .cap b { display:block; font-size:.74rem; color:var(--soft); font-weight:500; margin-bottom:.25rem; }
  .acts { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
  button { font:inherit; font-size:.84rem; font-weight:500; padding:.4rem .8rem;
           border-radius:4px; border:1px solid var(--ink); background:var(--ink);
           color:var(--paper); cursor:pointer; }
  button.ghost { background:transparent; color:var(--soft); border-color:var(--rule); }
  button:hover:not(:disabled) { opacity:.85; }
  button:disabled { opacity:.4; cursor:default; }
  button:focus-visible, nav a:focus-visible, .tabs a:focus-visible {
    outline:2px solid var(--signal); outline-offset:2px; }
  .bar { display:flex; align-items:center; gap:.55rem; font-size:.8rem;
         padding:.5rem .75rem; border-radius:4px; margin-bottom:1rem;
         color:var(--soft); background:#EAEEEC; }
  .bar span.dot { width:7px; height:7px; border-radius:50%; flex:none; }
  .bar.good span.dot { background:var(--done); }
  .bar.warn span.dot { background:var(--amber); }
  .bar.bad { background:#FBE9E7; color:#8C2A1E; }
  .bar.bad span.dot { background:var(--signal); }
  .bar.demo { background:#FFF6E0; color:#7A5600; }
  .bar.demo span.dot { background:var(--amber); }
  .btn-link { font:inherit; font-size:.84rem; font-weight:500; padding:.4rem .8rem;
              border-radius:4px; border:1px solid var(--ink); background:var(--ink);
              color:var(--paper); text-decoration:none; }
  .btn-link:hover { opacity:.85; }
  .ghost-link { font-size:.84rem; font-weight:500; padding:.4rem .8rem; border-radius:4px;
                border:1px solid var(--rule); color:var(--soft); text-decoration:none; }
  .ghost-link:hover { color:var(--ink); }
  .ig-link { font-size:.84rem; font-weight:500; padding:.4rem .8rem; border-radius:4px;
             border:1px solid var(--rule); color:var(--soft); text-decoration:none;
             margin-left:auto; }
  .ig-link:hover { color:var(--ink); border-color:var(--soft); }
  .gap { display:inline-block; width:14px; height:14px; line-height:14px;
         text-align:center; border-radius:50%; background:var(--amber);
         color:#fff; font-size:.62rem; font-weight:700; margin-right:.3rem; }
  .empty { padding:3rem 0; color:var(--soft); }
  .empty b { display:block; color:var(--ink); font-weight:600; margin-bottom:.3rem; }
  .ticketed { color:var(--done); font-size:.82rem; font-weight:500; text-decoration:none; }
  .failed { color:var(--signal); font-size:.82rem; font-weight:500; }
  .spin { display:inline-block; width:9px; height:9px; border-radius:50%;
          border:2px solid var(--paper); border-top-color:transparent;
          animation:sp .7s linear infinite; vertical-align:-1px; margin-right:.4rem; }
  @keyframes sp { to { transform:rotate(360deg); } }
  @media (max-width:720px) {
    body { font-size:16px; }
    header { padding:1rem; gap:.5rem; }
    h1 { font-size:1.25rem; }
    .tally { font-size:.78rem; width:100%; order:3; }
    .controls { width:100%; margin-left:0; order:2; justify-content:space-between; }
    .wrap { grid-template-columns:1fr; gap:1rem; padding:1rem 1rem 3rem; }

    /* The company rail becomes a swipeable strip. Fourteen stacked links
       would push the actual signals two screens down. */
    /* Horizontal rail on mobile: the label becomes a divider, its count is
       dropped because the chips beside it already carry the numbers. */
    .sector { flex:0 0 auto; margin:0 .2rem 0 .5rem; padding:.5rem 0 .5rem .6rem;
              border-bottom:none; border-left:1px solid var(--rule);
              align-items:center; }
    .sector span:last-child { display:none; }
    nav { position:static; display:flex; gap:.4rem; overflow-x:auto;
          padding-bottom:.5rem; margin:0 -1rem; padding-left:1rem;
          padding-right:1rem; -webkit-overflow-scrolling:touch;
          scrollbar-width:none; }
    nav::-webkit-scrollbar { display:none; }
    nav a { flex:0 0 auto; gap:.4rem; padding:.5rem .7rem; border-radius:999px;
            background:#E6EAE9; white-space:nowrap; min-height:38px;
            align-items:center; }
    nav a[aria-current] { background:var(--ink); }

    .tabs { gap:1rem; overflow-x:auto; scrollbar-width:none; }
    .tabs::-webkit-scrollbar { display:none; }
    .tabs a { white-space:nowrap; padding:.6rem 0 .7rem; }

    /* Let the header row wrap instead of crushing the title. */
    .top { flex-wrap:wrap; gap:.5rem .6rem; }
    .meta { margin-left:0; white-space:normal; width:100%; order:3;
            font-size:.75rem; }
    .meter { order:2; }
    .track { width:72px; }
    article { padding:.9rem 1rem; }
    h2 { font-size:1.02rem; line-height:1.35; }
    .sum { font-size:.92rem; }

    /* Thumb-sized targets, side by side. */
    .acts { gap:.5rem; }
    .acts button, .acts .ghost-link, .acts .ig-link {
      flex:1 1 auto; min-height:44px; text-align:center; padding:.6rem .8rem;
      display:flex; align-items:center; justify-content:center; }
    .acts .ig-link { margin-left:0; }
    .cap { max-width:none; }
    .bar { font-size:.78rem; align-items:flex-start; }
    .bar span.dot { margin-top:.4rem; }
  }
  @media (max-width:380px) {
    .acts button, .acts .ghost-link, .acts .ig-link { flex:1 1 100%; }
  }
  @media (prefers-reduced-motion:reduce) { .spin { animation:none; } }
"""

LIVE_JS = """
let polling = false;

function esc(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

async function act(id, what, btn) {
  const card = btn.closest('article');
  const acts = card.querySelector('.acts');
  const original = acts.innerHTML;
  acts.querySelectorAll('button').forEach(b => b.disabled = true);
  btn.innerHTML = '<span class="spin"></span>Working';
  try {
    const res = await fetch('/api/' + what + '/' + id, {method:'POST'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.detail || ('HTTP ' + res.status));
    if (what === 'ticket') {
      acts.innerHTML = '<a class="ticketed" href="' + esc(data.url || '#') +
                       '" target="_blank" rel="noopener">Opened in ClickUp &rarr;</a>';
    } else {
      card.remove();
    }
  } catch (err) {
    acts.innerHTML = original;
    const note = document.createElement('span');
    note.className = 'failed';
    note.textContent = err.message;
    acts.appendChild(note);
  }
}

async function refresh(btn) {
  if (polling) return;
  polling = true;
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span>Checking';
  try {
    const res = await fetch('/api/poll', {method:'POST'});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await watch();
  } catch (err) {
    btn.disabled = false; btn.innerHTML = label; polling = false;
    document.querySelector('.checked').textContent = 'Refresh failed: ' + err.message;
  }
}

async function watch() {
  for (let i = 0; i < 300; i++) {                 // ~10 minute ceiling
    await new Promise(r => setTimeout(r, 2000));
    const s = await (await fetch('/api/state')).json();
    if (!s.running) { location.reload(); return; }
  }
  location.reload();
}

async function setAuto(on) {
  await fetch('/api/autorefresh?on=' + (on ? 1 : 0), {method:'POST'});
}

// Auto-refresh: ask the server whether a cycle is due, rather than each open
// tab running its own clock and stacking duplicate polls.
function startAutoLoop(minutes) {
  if (!minutes) return;
  setInterval(async () => {
    if (polling || document.hidden) return;
    try {
      const s = await (await fetch('/api/state')).json();
      if (s.auto_refresh && s.due && !s.running) {
        polling = true;
        await fetch('/api/poll', {method:'POST'});
        await watch();
      }
    } catch (e) { /* offline; try again next tick */ }
  }, 60000);
}
"""

SNAPSHOT_JS = """
// A static page cannot refresh itself, so it has to be honest about its age.
// Without this, a snapshot dropped on Netlify and forgotten looks identical to
// a live dashboard, and the shows listed on it will have already happened.
(function ageCheck() {
  const el = document.querySelector('[data-built]');
  if (!el) return;
  const built = new Date(el.dataset.built);
  const hours = (Date.now() - built) / 36e5;
  if (hours < 36) return;
  const days = Math.floor(hours / 24);
  const bar = document.createElement('div');
  bar.className = 'bar bad';
  bar.innerHTML = '<span class="dot"></span>' +
    'This page was built ' + (days < 1 ? Math.floor(hours) + ' hours' : days + ' days') +
    ' ago and has not updated since. Nothing here is current. ' +
    'Set up the daily job, or rebuild and re-upload it.';
  const main = document.querySelector('main');
  main.insertBefore(bar, main.firstChild);
})();

async function copyCaption(btn) {
  const text = btn.dataset.caption || '';
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // Clipboard API needs https or localhost; fall back to a selectable prompt.
    window.prompt('Copy the caption:', text);
    return;
  }
  const original = btn.textContent;
  btn.textContent = 'Copied';
  setTimeout(() => { btn.textContent = original; }, 1600);
}
"""

DEMO_JS = """
function demoFilter(el, kind) {
  const value = el.dataset.value;
  const group = kind === 'status' ? '.tabs a' : 'nav a';
  document.querySelectorAll(group).forEach(a => a.removeAttribute('aria-current'));
  el.setAttribute('aria-current', 'page');
  if (kind === 'status') window.demoStatus = value; else window.demoEntity = value;
  document.querySelectorAll('article').forEach(card => {
    const okStatus = !window.demoStatus || window.demoStatus === 'all'
                     || card.dataset.status === window.demoStatus;
    const okEntity = !window.demoEntity || card.dataset.entity === window.demoEntity;
    card.style.display = (okStatus && okEntity) ? '' : 'none';
  });
  return false;
}
window.demoStatus = 'pending';
"""

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GL Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;800&display=swap" rel="stylesheet">
<style>__STYLE__</style></head><body>
<header>
  <h1>GL Radar</h1>
  <span class="tally">__TALLY__</span>
  <div class="controls">__CONTROLS__</div>
</header>
<div class="wrap">
  <nav>__NAV__</nav>
  <main>
    __STATUSBAR__
    <div class="tabs">__TABS__</div>
    __ROWS__
  </main>
</div>
<script>__SCRIPT__</script>
</body></html>"""


def _ago(iso: str) -> str:
    try:
        when = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        mins = (datetime.now(timezone.utc) - when).total_seconds() / 60
    except ValueError:
        return ""
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{int(mins)}m ago"
    if mins < 1440:
        return f"{int(mins // 60)}h ago"
    return f"{int(mins // 1440)}d ago"


def _row_html(r: dict, demo: bool, snapshot: bool = False) -> str:
    e = html.escape
    entity = pipe.by_key.get(r["entity_key"])
    hot = " hot" if r["importance"] >= 85 else ""
    caption, angle = r.get("caption") or "", r.get("repost_angle") or ""

    if r["status"] == "ticketed":
        actions = (f'<a class="ticketed" href="{e(r.get("clickup_url") or "#")}" '
                   f'target="_blank" rel="noopener">In ClickUp &rarr;</a>')
    elif r["status"] in ("dismissed", "expired"):
        label = "Passed on" if r["status"] == "dismissed" else "Aged out of the window"
        actions = f'<span class="meta" style="margin:0">{label}</span>'
    elif snapshot:
        # No backend to click against, so give the two things that still work
        # without one: the caption on the clipboard and the source in a tab.
        caption_attr = html.escape(caption or "", quote=True)
        label = {
            "instagram": "Open Instagram post",
            "events": "Event page",
            "website": "Open page",
        }.get(r["source"], "Open article")

        # A link to the brand's profile, not to a specific post. Reading posts
        # needs a Meta app and a token on the @grosslabs account; jumping to the
        # profile needs neither, and lands you where the repostable content is
        # anyway. Cheap, and it never expires.
        ig = ""
        if entity and entity.instagram:
            handle = entity.instagram[0]
            ig = (f'<a class="ig-link" href="https://www.instagram.com/{e(handle)}/" '
                  f'target="_blank" rel="noopener" '
                  f'title="Open @{e(handle)} to find the post to repost">'
                  f'@{e(handle)}</a>')

        actions = (f'<button onclick="copyCaption(this)" data-caption="{caption_attr}"'
                   f'{" disabled" if not caption else ""}>Copy caption</button>'
                   f'<a class="ghost-link" href="{e(r.get("url") or "#")}" '
                   f'target="_blank" rel="noopener">{label}</a>{ig}')
    elif demo:
        actions = ('<button disabled>Create repost ticket</button>'
                   '<button class="ghost" disabled>Not for us</button>'
                   '<span class="meta" style="margin:0">Preview only</span>')
    else:
        actions = (f'<button onclick="act({r["id"]},\'ticket\',this)">Create repost ticket</button>'
                   f'<button class="ghost" onclick="act({r["id"]},\'dismiss\',this)">Not for us</button>')

    detail = ""
    if caption or angle:
        detail = ("<details><summary>Angle and caption</summary>"
                  + (f'<div class="cap"><b>Lead with</b>{e(angle)}</div>' if angle else "")
                  + (f'<div class="cap"><b>Suggested caption</b>{e(caption)}</div>' if caption else "")
                  + "</details>")

    return f"""<article data-status="{e(r.get('view') or r['status'])}" data-entity="{e(r['entity_key'])}">
  <div class="top">
    <span class="co">{e(entity.name if entity else r["entity_key"])}</span>
    <span class="meter"><span class="track"><span class="fill{hot}" style="width:{r['importance']}%"></span></span>
      <span class="score">{r['importance']}</span></span>
    <span class="meta">{e(r.get('category') or '')} &middot; {e(r.get('author') or r['source'])} &middot; {_ago(r.get('published_at',''))}</span>
  </div>
  <h2><a href="{e(r.get('url') or '#')}" target="_blank" rel="noopener">{e(r['title'])}</a></h2>
  <p class="sum">{e(r.get('summary') or '')}</p>
  {detail}
  <div class="acts">{actions}</div>
</article>"""


def _statusbar(window, demo: bool, snapshot: bool = False) -> str:
    """Say what is true, including when the answer is "I am not watching".

    An empty queue and a broken pipeline look identical from the outside. The
    top of the page is where that ambiguity has to be resolved, every time.
    """
    from .coverage import gaps, pipeline_ready, report as coverage_report

    if demo:
        return ('<div class="bar demo"><span class="dot"></span>'
                'Static preview with sample data. The Refresh button and the ticket '
                'buttons only work in the running app: python -m radar.cli serve</div>')

    if snapshot:
        cov = coverage_report(pipe.conn, pipe.settings, pipe.entities)
        watched = sum(1 for c in cov if c.status == "ok")
        problems = gaps(cov)
        built = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        stamp = datetime.now(timezone.utc).isoformat()
        bars = [("good", f"Built {built} · watching {watched} of {len(cov)} companies · "
                         f"{window.describe()}")]
        if problems:
            bars.append(("warn", f"Not fully covered: "
                                 f"{', '.join(c.name for c in problems)}"))
        bars.append(("demo", "Read-only snapshot. Tickets are created automatically in "
                             "ClickUp by the scheduled job; this page is for browsing."))
        out = "".join(
            f'<div class="bar {tone}"><span class="dot"></span>{html.escape(text)}</div>'
            for tone, text in bars)
        # Carries the build time for the client-side staleness check above.
        return f'<div data-built="{stamp}"></div>' + out

    bars = []
    ready, why = pipeline_ready(pipe.settings)
    if not ready:
        bars.append(("bad", f"Not monitoring. {why}"))
    elif why:
        bars.append(("warn", why))

    cov = coverage_report(pipe.conn, pipe.settings, pipe.entities)
    problems = gaps(cov)
    if problems:
        names = ", ".join(c.name for c in problems[:4])
        more = f" and {len(problems) - 4} more" if len(problems) > 4 else ""
        bars.append(("warn", f"{len(problems)} company/companies not fully covered: "
                             f"{names}{more}. Run: radar doctor"))

    report = pipe.supervise(repair=True)
    failures = [c for c in report.checks
                if c.status == "fail" and c.name != "company_coverage"]
    if failures:
        bars.append(("bad", "; ".join(f"{c.name}: {c.detail}" for c in failures)))

    if not bars:
        upcoming = next((c.detail for c in report.checks if c.name == "events_covered"), "")
        watched = sum(1 for c in cov if c.status == "ok")
        bars.append(("good", f"Watching {watched} companies · {window.describe()} · "
                             f"{upcoming} · {pipe.clickup.verify()}"))

    return "".join(f'<div class="bar {tone}"><span class="dot"></span>{html.escape(text)}</div>'
                   for tone, text in bars)


def render(status: str = "pending", entity: str = "", demo: bool = False,
           rows: list[dict] | None = None, snapshot: bool = False) -> str:
    """demo     sample data, everything disabled
    snapshot   real data, read-only, safe to host on a static site
    otherwise  the live app"""
    static = demo or snapshot
    window = pipe.refresh_window()
    since = window.oldest_allowed.isoformat()
    floor = pipe.settings["scoring"]["min_importance"]

    archiving = status == "archive"
    if rows is None:
        if static:
            # Both views are baked in; the tabs filter client-side.
            review_rows = db.feed(pipe.conn, status="all", entity=entity,
                                  min_importance=floor, since=since,
                                  view="review", limit=200)
            archive_rows = db.feed(pipe.conn, status="all", entity=entity,
                                   min_importance=floor, since=since,
                                   view="archive",
                                   archive_since=window.archive_floor.isoformat(),
                                   limit=300)
            for r in review_rows:
                r["view"] = "pending"
            for r in archive_rows:
                r["view"] = "archive"
            rows = review_rows + archive_rows
        else:
            rows = db.feed(
                pipe.conn,
                status="all" if archiving else status,
                entity=entity, min_importance=floor, since=since,
                view="archive" if archiving else "review",
                archive_since=window.archive_floor.isoformat(),
                limit=300 if archiving else 200)
    c = db.counts(pipe.conn)
    tally = (f"<b>{c['relevant']}</b> signals from <b>{c['items']}</b> scanned "
             f"&middot; <b>{c['ticketed']}</b> ticketed &middot; <b>{c['expired']}</b> aged out")

    per_entity: dict[str, int] = {}
    for r in db.feed(pipe.conn, status="pending", limit=500,
                     min_importance=floor, since=since):
        per_entity[r["entity_key"]] = per_entity.get(r["entity_key"], 0) + 1

    # In demo mode every control is a client-side filter, so nothing ever
    # resolves against a server that is not there.
    from .coverage import report as coverage_report
    cov_by_key = {c.key: c for c in coverage_report(pipe.conn, pipe.settings, pipe.entities)}

    def nav_link(key: str, label: str, count: int) -> str:  # noqa: C901
        current = (" aria-current='page'" if (entity == key or (not entity and not key)) else "")
        cov = cov_by_key.get(key)
        # A company with no working source gets a visible mark. Zero next to a
        # name should never be readable as "nothing happened" when the truth is
        # "nothing was looked at".
        warn = ""
        if cov and cov.status != "ok":
            warn = (f' title="{html.escape(cov.headline)}"')
        badge = (f'<span class="gap">!</span>' if warn else "")
        if static:
            return (f'<a href="#" data-value="{html.escape(key)}" '
                    f'onclick="return demoFilter(this,\'entity\')"{current}{warn}>'
                    f'<span>{html.escape(label)}</span><span>{badge}{count}</span></a>')
        query = f"/?status={status}" + (f"&entity={key}" if key else "")
        return (f'<a href="{query}"{current}{warn}>'
                f'<span>{html.escape(label)}</span><span>{badge}{count}</span></a>')

    # Grouped by sector. Twenty-eight flat names is a wall; six labelled
    # groups is something you can scan, and the grouping predicts what kind
    # of news each company produces.
    ORDER = ["Music", "Platform", "Sport", "Consumer", "Hospitality", "Other"]
    by_sector: dict[str, list] = {}
    for key, ent in pipe.by_key.items():
        by_sector.setdefault(getattr(ent, "sector", "Other"), []).append((key, ent))

    nav = [nav_link("", "Everything", sum(per_entity.values()))]
    for sector in ORDER:
        members = by_sector.get(sector)
        if not members:
            continue
        total = sum(per_entity.get(k, 0) for k, _ in members)
        nav.append(f'<div class="sector"><span>{html.escape(sector)}</span>'
                   f'<span>{total}</span></div>')
        for key, ent in sorted(members, key=lambda kv: -per_entity.get(kv[0], 0)):
            nav.append(nav_link(key, ent.name, per_entity.get(key, 0)))

    def tab_link(value: str, label: str) -> str:
        current = " aria-current='page'" if status == value else ""
        if static:
            return (f'<a href="#" data-value="{value}" '
                    f'onclick="return demoFilter(this,\'status\')"{current}>{label}</a>')
        query = f"/?status={value}" + (f"&entity={entity}" if entity else "")
        return f'<a href="{query}"{current}>{label}</a>'

    tabs = "".join(tab_link(v, l) for v, l in TABS)

    visible = rows
    if visible:
        body = "".join(_row_html(r, static, snapshot) for r in visible)
    else:
        action = ("" if static else
                  '<button onclick="refresh(this)">Check for updates</button>')
        if archiving:
            body = (f'<div class="empty"><b>Archive is empty.</b>'
                    f'Items move here once they fall out of the '
                    f'{window.lookback_days}-day window, and stay for '
                    f'{window.archive_days} days.</div>')
        else:
            body = (f'<div class="empty"><b>Nothing waiting.</b>'
                    f'Showing the last {window.lookback_days} days and the next '
                    f'{window.lookahead_days} days of shows. {action}</div>')

    if snapshot:
        # No refresh control. The job runs every morning on its own; a button
        # here would only ever be a link to GitHub, which is not worth the
        # space it takes up.
        built = datetime.now(timezone.utc)
        controls = (f'<span class="checked">Updated {_ago(built.isoformat())}</span>'
                    f'<span class="auto">Rebuilds daily at 07:00</span>')
        script = DEMO_JS + SNAPSHOT_JS
    elif demo:
        controls = ('<span class="checked">Sample data</span>'
                    '<label class="auto"><input type="checkbox" checked disabled>'
                    'Auto every 60m</label>'
                    '<button disabled title="Works in the running app">Refresh</button>')
        script = DEMO_JS
    else:
        auto_on = pipe.auto_refresh_enabled()
        every = pipe.settings["poll"].get("auto_refresh_minutes", 60)
        last = db.get_cursor(pipe.conn, "last_poll_at")
        controls = (
            f'<span class="checked">Checked {_ago(last) if last else "never"}</span>'
            f'<label class="auto"><input type="checkbox" '
            f'{"checked" if auto_on else ""} onchange="setAuto(this.checked)">'
            f'Auto every {every}m</label>'
            f'<button onclick="refresh(this)">Refresh</button>')
        script = LIVE_JS + f"\nstartAutoLoop({every});\n"

    return (PAGE.replace("__STYLE__", STYLE).replace("__TALLY__", tally)
            .replace("__CONTROLS__", controls).replace("__NAV__", "".join(nav))
            .replace("__STATUSBAR__", _statusbar(window, demo, snapshot))
            .replace("__TABS__", tabs).replace("__ROWS__", body)
            .replace("__SCRIPT__", script))


# ---------------- routes ----------------

@app.get("/", response_class=HTMLResponse)
def index(status: str = "pending", entity: str = ""):
    return render(status=status, entity=entity, demo=False)


@app.post("/api/ticket/{item_id}")
def api_ticket(item_id: int):
    try:
        task_id, url = pipe.ticket(item_id)
        return {"ok": True, "id": task_id, "url": url}
    except Exception as exc:  # noqa: BLE001
        log.warning("ticket %s failed: %s", item_id, exc)
        return JSONResponse({"ok": False, "detail": str(exc)[:200]}, status_code=400)


@app.post("/api/dismiss/{item_id}")
def api_dismiss(item_id: int):
    pipe.dismiss(item_id)
    return {"ok": True}


def _run_poll(group: str = "all") -> None:
    """Collection runs off the request thread so the button returns instantly
    instead of hanging for the length of a full classification pass."""
    if not _poll_lock.acquire(blocking=False):
        log.info("poll already running, skipping")
        return
    _poll_state.update(running=True, started_at=datetime.now(timezone.utc).isoformat(),
                       error="")
    try:
        _poll_state["result"] = pipe.run_cycle(group)
    except Exception as exc:  # noqa: BLE001
        log.exception("poll failed")
        _poll_state["error"] = str(exc)[:200]
    finally:
        _poll_state["running"] = False
        _poll_lock.release()


@app.post("/api/poll")
def api_poll(group: str = "all"):
    if _poll_state["running"]:
        return {"started": False, "reason": "already running"}
    threading.Thread(target=_run_poll, args=(group,), daemon=True).start()
    return {"started": True}


@app.get("/api/state")
def api_state():
    last = db.get_cursor(pipe.conn, "last_poll_at")
    return {
        "running": _poll_state["running"],
        "last_poll_at": last,
        "last_result": _poll_state.get("result"),
        "error": _poll_state.get("error", ""),
        "auto_refresh": pipe.auto_refresh_enabled(),
        "due": pipe.poll_is_due(),
        "window": pipe.window.describe(),
    }


@app.post("/api/autorefresh")
def api_autorefresh(on: int = 1):
    pipe.set_auto_refresh(bool(on))
    return {"ok": True, "auto_refresh": bool(on)}


@app.get("/api/health")
def api_health():
    """Machine-readable supervisor output, for cron or an uptime check."""
    report = pipe.supervise(repair=False)
    return JSONResponse(
        {"ok": report.ok, "worst": report.worst, "window": report.window.describe(),
         "checks": [{"name": c.name, "status": c.status, "detail": c.detail}
                    for c in report.checks]},
        status_code=200 if report.ok else 503)


@app.on_event("startup")
def start_scheduler():
    p = pipe.settings["poll"]
    # Intervals are wall-clock agnostic, but the daily sweep is a promise about
    # a time of day, so the scheduler runs in the user's timezone.
    tz = pipe.settings.get("notify", {}).get("timezone", "UTC")
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(lambda: _run_poll("news"), "interval", minutes=p["news_minutes"])
    sched.add_job(lambda: _run_poll("website"), "interval", minutes=p["website_minutes"])
    sched.add_job(lambda: _run_poll("instagram"), "interval", minutes=p["instagram_minutes"])
    sched.add_job(lambda: _run_poll("linkedin"), "interval", minutes=p["linkedin_minutes"])
    sched.add_job(lambda: _run_poll("events"), "interval", minutes=p["events_minutes"])
    # A guaranteed full sweep once a day, so the board is never more than
    # 24 hours stale even if every other trigger was missed.
    hour = int(p.get("daily_sweep_hour", 6))
    sched.add_job(lambda: _run_poll("all"), "cron", hour=hour, minute=0)
    sched.add_job(lambda: pipe.supervise(repair=True), "interval",
                  minutes=p.get("supervisor_minutes", 60))
    sched.start()
    log.info("Scheduler running · %s · daily sweep at %02d:00 %s",
             pipe.window.describe(), hour, tz)
