"""Command line: python -m radar.cli <command>"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


def cmd_poll(args) -> None:
    from .pipeline import Pipeline
    p = Pipeline()
    result = p.run_cycle(args.group)
    print(f"\n{result['new_items']} new items, {result['surfaced']} worth a look, "
          f"{result['notified']} notified")
    if result.get("repairs"):
        print(f"Supervisor repaired {result['repairs']} issue(s)")
    p.close()
    # Non-zero exit lets cron surface a degraded run instead of swallowing it.
    sys.exit(0 if result.get("supervisor") != "fail" else 1)


def cmd_supervise(args) -> None:
    """Audit the recency contract. Exits 1 on violation so cron can alarm."""
    from .pipeline import Pipeline
    p = Pipeline()
    report = p.supervise(repair=not args.check_only)
    print()
    print(report.to_text())
    p.close()
    sys.exit(0 if report.ok else 1)


def cmd_serve(args) -> None:
    import uvicorn
    from .config import load_settings
    s = load_settings()["server"]
    host, port = args.host or s["host"], args.port or s["port"]
    print(f"\n  GL Radar → http://{host}:{port}\n")
    uvicorn.run("radar.server:app", host=host, port=port, log_level="warning")


def cmd_doctor(args) -> None:
    """Say plainly what is being watched, what is blind, and why."""
    from . import db
    from .config import load_entities, load_settings
    from .coverage import gaps, pipeline_ready, report, system_readiness
    from .freshness import load_window

    settings, entities = load_settings(), load_entities()
    window = load_window(settings)
    conn = db.connect()
    db.init(conn)

    print("\nPipeline")
    ready, why = pipeline_ready(settings)
    mark = "ok  " if ready and not why else ("warn" if ready else "FAIL")
    print(f"  {mark} classifier    {why or 'Claude, ready'}")
    from .sinks.clickup import ClickUp
    cu = ClickUp(settings)
    print(f"  {'ok  ' if cu.enabled else 'warn'} clickup       "
          f"{'configured' if cu.enabled else 'no token or list — tickets cannot be created'}")

    print("\nCollectors")
    for name, state in sorted(system_readiness(settings).items()):
        if not state.enabled:
            mark, note = "off ", "disabled in settings.yaml"
        elif not state.credentialed:
            mark, note = "FAIL", state.reason
        else:
            mark, note = "ok  ", "ready"
        print(f"  {mark} {name:<14} {note}")

    print(f"\nCompanies ({window.describe()})")
    cov = report(conn, settings, entities, since=window.oldest_allowed.isoformat())
    marks = {"ok": "ok  ", "thin": "thin", "blocked": "FAIL", "blind": "FAIL"}
    for c in sorted(cov, key=lambda c: (c.priority, c.name)):
        seen = c.last_item_at[:10] if c.last_item_at else "never"
        print(f"  {marks[c.status]} p{c.priority} {c.name:<26} last item {seen:<12} {c.headline}")

    problems = gaps(cov)
    print()
    if problems:
        print(f"{len(problems)} company/companies cannot be monitored properly:\n")
        for c in problems:
            print(f"  {c.name}")
            print(f"    {c.headline}")
            if c.status == "blind":
                print("    Fix: add `domains` or `watch_pages` for it in config/companies.yaml")
            elif c.status == "thin":
                print("    Fix: find its official site or feed and add `watch_pages`")
            else:
                print("    Fix: add the missing key to .env")
        print()
    else:
        print("Every company has a working source.\n")
    conn.close()
    sys.exit(1 if any(c.status in ("blind", "blocked") for c in cov) else 0)


def cmd_lists(args) -> None:
    """Print every ClickUp list you can write to, with its id."""
    from .config import load_settings
    from .sinks.clickup import ClickUp, ClickUpError
    cu = ClickUp(load_settings())
    try:
        lists = cu.discover()
    except ClickUpError as exc:
        print(f"\n{exc}")
        sys.exit(1)
    if not lists:
        print("\nNo lists found. Check that CLICKUP_TOKEN has workspace access.")
        sys.exit(1)
    print(f"\n{len(lists)} list(s) available:\n")
    width = max(len(l["name"]) for l in lists)
    for entry in lists:
        print(f"  {entry['id']:<14} {entry['name']:<{width}}  {entry['path']}")
    print("\nPaste the id into clickup.list_id in config/settings.yaml,")
    print("or into clickup.list_overrides to route one company elsewhere.")
    print(f"\nCurrently: {cu.verify()}")


def cmd_export(args) -> None:
    """Build a static site you can host anywhere, including Netlify."""
    from .server import render
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(status="pending", snapshot=True))
    # Netlify serves any file it finds; a 404 page keeps stray URLs tidy.
    (out / "404.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Not found</title>"
        "<body style='font-family:system-ui;padding:3rem'>"
        "<h1>Not found</h1><p><a href='/'>Back to GL Radar</a></p>")
    (out / "_headers").write_text(
        "/*\n"
        "  X-Frame-Options: DENY\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: no-referrer\n"
        # The queue names unannounced deals; keep it out of search results.
        "  X-Robots-Tag: noindex, nofollow\n")
    (out / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    size = (out / "index.html").stat().st_size
    print(f"\nWrote {out.resolve()}  ({size:,} bytes)")
    print("Drag this folder onto app.netlify.com/drop to publish it.")
    print("Read-only: captions copy to the clipboard, tickets come from the scheduled job.")


def cmd_preview(args) -> None:
    """Write a standalone HTML preview that works without the server running."""
    from .server import render
    out = Path(args.out)
    out.write_text(render(status="pending", demo=True))
    print(f"\nWrote {out.resolve()}")
    print("This is a static snapshot. Buttons and refresh need: python -m radar.cli serve")


def cmd_status(args) -> None:
    from . import db
    from .config import load_entities, load_settings
    from .freshness import load_window
    conn = db.connect()
    db.init(conn)
    window = load_window(load_settings())
    c = db.counts(conn)
    print(f"\nWindow           {window.describe()}")
    last = db.get_cursor(conn, "last_poll_at")
    print(f"Last checked     {last[:16].replace('T', ' ') if last else 'never'}")
    print(f"Items scanned    {c['items']}")
    print(f"Relevant signals {c['relevant']}")
    print(f"Ticketed         {c['ticketed']}")
    print(f"Passed on        {c['dismissed']}")
    print(f"Aged out         {c['expired']}")
    print(f"\nTracking {len(load_entities())} entities\n")
    print("Last collector runs")
    for r in conn.execute(
        """SELECT collector, MAX(started_at) AS at, ok, found, new, error
           FROM runs GROUP BY collector ORDER BY at DESC"""):
        flag = "ok  " if r["ok"] else "FAIL"
        print(f"  {flag} {r['collector']:<14} {r['at'][:16]}  "
              f"{r['found']:>3} found / {r['new']:>3} new  {r['error'][:60]}")
    conn.close()


def cmd_test(args) -> None:
    """Dry run one entity end to end without writing to ClickUp."""
    from .pipeline import Pipeline
    p = Pipeline()
    if args.entity not in p.by_key:
        print(f"Unknown entity. Options: {', '.join(p.by_key)}")
        sys.exit(1)
    print(f"Collecting for {p.by_key[args.entity].name}...")
    p.collect("news", only=args.entity)
    for s in p.classify_pending(limit=args.limit):
        print(f"\n  [{s['importance']:>3}] {s['entity_name']} · {s.get('category')}")
        print(f"        {s['title'][:100]}")
        print(f"        {s.get('summary','')[:110]}")
    p.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="radar", description="GL Radar")
    sub = parser.add_subparsers(dest="cmd", required=True)

    poll = sub.add_parser("poll", help="run one collection cycle now")
    poll.add_argument("--group", default="all",
                      choices=["all", "news", "website", "instagram", "linkedin", "events"])
    poll.set_defaults(func=cmd_poll)

    serve = sub.add_parser("serve", help="start the review dashboard and scheduler")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("status", help="show what the system has been doing").set_defaults(func=cmd_status)

    sub.add_parser("doctor", help="what is being watched, what is blind, and why").set_defaults(func=cmd_doctor)

    sub.add_parser("lists", help="print your ClickUp lists and their ids").set_defaults(func=cmd_lists)

    exp = sub.add_parser("export", help="build a static site for Netlify or any host")
    exp.add_argument("--out", default="public")
    exp.set_defaults(func=cmd_export)

    prev = sub.add_parser("preview", help="write a standalone HTML preview")
    prev.add_argument("--out", default="preview.html")
    prev.set_defaults(func=cmd_preview)

    sup = sub.add_parser("supervise", help="audit and repair the recency contract")
    sup.add_argument("--check-only", action="store_true",
                     help="report violations without repairing them")
    sup.set_defaults(func=cmd_supervise)

    test = sub.add_parser("test", help="dry run one entity")
    test.add_argument("entity")
    test.add_argument("--limit", type=int, default=10)
    test.set_defaults(func=cmd_test)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
