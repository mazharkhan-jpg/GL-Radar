"""Exercise the real HTTP endpoints every button calls.

Boots the actual server on a spare port and clicks everything programmatically.
The offline smoke test proves the logic; this proves the wiring.

Run: python scripts/smoke_test.py && python scripts/live_test.py
"""
import os, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RADAR_DB",
                      str(Path(__file__).resolve().parent.parent / "data" / "test.db"))
import uvicorn, httpx
from radar.server import app

cfg = uvicorn.Config(app, host="127.0.0.1", port=8791, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()
for _ in range(60):
    try:
        httpx.get("http://127.0.0.1:8791/api/state", timeout=1); break
    except Exception: time.sleep(0.5)

B = "http://127.0.0.1:8791"
fails = []
def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok: fails.append(label)

print("Page routes")
r = httpx.get(B + "/")
check("GET / returns 200", r.status_code == 200)
check("refresh button present", 'onclick="refresh(this)"' in r.text)
check("auto-refresh toggle present", 'setAuto(this.checked)' in r.text)
check("last-checked shown", 'class="checked"' in r.text)

for status in ["pending", "ticketed", "dismissed", "expired", "all"]:
    rr = httpx.get(f"{B}/?status={status}")
    check(f"tab '{status}' resolves", rr.status_code == 200)

rr = httpx.get(B + "/?status=pending&entity=beeup")
check("brand filter resolves", rr.status_code == 200 and "BEEUP" in rr.text)
check("tab links are same-origin paths", 'href="/?status=ticketed"' in r.text)

print("\nActions")
import re
ids = re.findall(r"act\((\d+),'ticket'", r.text)
check("ticket buttons rendered", bool(ids), f"{len(ids)} found")
if ids:
    rr = httpx.post(f"{B}/api/ticket/{ids[0]}")
    body = rr.json()
    # No ClickUp token in the test env, so the correct behaviour is a clean,
    # readable 400 rather than a silent nothing.
    check("ticket returns a readable error, not silence",
          rr.status_code == 400 and "CLICKUP_TOKEN" in body.get("detail", ""),
          body.get("detail", "")[:50])
    rr = httpx.post(f"{B}/api/dismiss/{ids[-1]}")
    check("dismiss succeeds", rr.status_code == 200 and rr.json().get("ok"))

print("\nRefresh plumbing")
rr = httpx.get(B + "/api/state"); st = rr.json()
check("/api/state responds", rr.status_code == 200)
check("state reports auto_refresh", "auto_refresh" in st, f"auto={st.get('auto_refresh')}")
check("state reports due flag", "due" in st, f"due={st.get('due')}")

rr = httpx.post(B + "/api/poll")
check("/api/poll returns immediately", rr.status_code == 200 and rr.json().get("started"))
rr2 = httpx.post(B + "/api/poll")
check("second poll is refused while running",
      rr2.json().get("started") is False or not rr2.json().get("started"))

rr = httpx.post(B + "/api/autorefresh?on=0")
check("auto-refresh can be turned off", rr.json().get("auto_refresh") is False)
rr = httpx.post(B + "/api/autorefresh?on=1")
check("auto-refresh can be turned back on", rr.json().get("auto_refresh") is True)

rr = httpx.get(B + "/api/health")
check("/api/health responds", rr.status_code in (200, 503), f"status={rr.status_code}")

print("\nDemo preview")
from radar.server import render
demo = render(status="pending", demo=True)
check("no server-relative hrefs", 'href="/?status=' not in demo)
check("all controls are client-side", demo.count("demoFilter") > 5)
check("actions disabled", "Preview only" in demo)
check("explains itself", "static preview" in demo.lower())
check("no fetch calls at all", "fetch(" not in demo)



print("\nStatic export (Netlify safety)")
import subprocess, tempfile, os as _os, pathlib
tmp = tempfile.mkdtemp()
subprocess.run([sys.executable, "-m", "radar.cli", "export", "--out", tmp],
               check=True, capture_output=True,
               cwd=str(pathlib.Path(__file__).resolve().parent.parent))
html_out = pathlib.Path(tmp, "index.html").read_text()
check("index.html written", len(html_out) > 5000, f"{len(html_out):,} bytes")
check("no fetch calls (would 404 on a static host)", "fetch(" not in html_out)
check("no server-relative links", 'href="/?status=' not in html_out)
check("no /api/ references", "/api/" not in html_out)
check("real data, not fixtures", "Warped Tour" in html_out or "Breakaway" in html_out)
check("copy-caption action present", "copyCaption" in html_out)
check("says it is read-only", "Read-only snapshot" in html_out)
check("noindex headers written", "noindex" in pathlib.Path(tmp, "_headers").read_text())
check("robots.txt disallows all", "Disallow: /" in pathlib.Path(tmp, "robots.txt").read_text())
check("tabs filter client-side", html_out.count("demoFilter") > 5)

print()
if fails:
    print(f"FAILED ({len(fails)}): " + ", ".join(fails)); sys.exit(1)
print("All live endpoint checks passed.")
