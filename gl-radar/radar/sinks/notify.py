"""Tell a human something happened."""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("radar.notify")


class Notifier:
    def __init__(self, settings: dict):
        self.cfg = settings.get("notify", {})
        self.server = settings.get("server", {})

    def _in_quiet_hours(self) -> bool:
        tz = ZoneInfo(self.cfg.get("timezone", "UTC"))
        hour = datetime.now(tz).hour
        start, end = self.cfg.get("quiet_start", 23), self.cfg.get("quiet_end", 8)
        return hour >= start or hour < end if start > end else start <= hour < end

    def send(self, title: str, message: str, url: str = "", force: bool = False) -> None:
        if self._in_quiet_hours() and not force:
            log.info("Quiet hours, holding: %s", title)
            return
        if self.cfg.get("desktop", True):
            self._desktop(title, message)
        if self.cfg.get("slack_webhook"):
            self._slack(title, message, url)

    @staticmethod
    def _desktop(title: str, message: str) -> None:
        system = platform.system()
        try:
            if system == "Darwin":
                safe_t, safe_m = title.replace('"', "'"), message.replace('"', "'")[:220]
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{safe_m}" with title "{safe_t}" sound name "Ping"'],
                    check=False, timeout=10)
            elif system == "Linux" and shutil.which("notify-send"):
                subprocess.run(["notify-send", title, message[:220]], check=False, timeout=10)
            elif system == "Windows":
                ps = (f'[Windows.UI.Notifications.ToastNotificationManager]::'
                      f'CreateToastNotifier("GL Radar")')
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                f'Write-Host "{title}: {message[:200]}"; {ps}'],
                               check=False, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.debug("desktop notify unavailable: %s", exc)

    def _slack(self, title: str, message: str, url: str) -> None:
        dash = f"http://{self.server.get('host','127.0.0.1')}:{self.server.get('port',8787)}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": f"*{title}*\n{message}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"<{url}|Open source> · <{dash}|Review queue>"}]},
        ]
        try:
            httpx.post(self.cfg["slack_webhook"],
                       json={"text": f"{title} — {message}", "blocks": blocks},
                       timeout=15)
        except Exception as exc:  # noqa: BLE001
            log.warning("slack notify failed: %s", exc)
