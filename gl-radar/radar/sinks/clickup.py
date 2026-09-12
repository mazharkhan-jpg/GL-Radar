"""Create the repost ticket in ClickUp.

Destination resolution, in order:
  1. `list_overrides[entity_key]` in settings.yaml — send BEEUP news to the
     brand team's list and Breakaway news to the events list
  2. `list_id` in settings.yaml
  3. CLICKUP_LIST_ID in .env

`ClickUp.discover()` walks your workspace and prints every list with its id, so
you never have to dig one out of a browser URL.
"""
from __future__ import annotations

import logging

import httpx

from ..config import env

log = logging.getLogger("radar.clickup")
API = "https://api.clickup.com/api/v2"


class ClickUpError(RuntimeError):
    """Raised with a message written for a human, not a stack trace."""


class ClickUp:
    def __init__(self, settings: dict):
        self.cfg = settings.get("clickup", {}) or {}
        self.token = env("CLICKUP_TOKEN")
        self.default_list = self.cfg.get("list_id") or env("CLICKUP_LIST_ID")
        self.overrides = self.cfg.get("list_overrides", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.default_list)

    def list_for(self, entity_key: str) -> str:
        return str(self.overrides.get(entity_key) or self.default_list or "")

    def _headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json"}

    # ---- the main event ----

    def create_task(self, row: dict, entity_name: str) -> tuple[str, str]:
        """Returns (task_id, task_url). Raises ClickUpError with a readable
        reason, because "nothing happened" is the worst possible failure mode
        for a button."""
        if not self.token:
            raise ClickUpError("CLICKUP_TOKEN is not set in .env")
        list_id = self.list_for(row.get("entity_key", ""))
        if not list_id:
            raise ClickUpError("No ClickUp list configured. Run: python -m radar.cli lists")

        payload = {
            "name": f"{self.cfg.get('task_name_prefix', '')}{entity_name}: {row['title']}"[:255],
            "markdown_description": self._body(row, entity_name),
            "tags": [t for t in list(self.cfg.get("default_tags", [])) +
                     [row.get("category", "")] if t],
            "priority": self._priority(row.get("importance", 0)),
        }
        fields = self._custom_fields(row, entity_name)
        if fields:
            payload["custom_fields"] = fields

        try:
            resp = httpx.post(f"{API}/list/{list_id}/task",
                              headers=self._headers(), json=payload, timeout=30)
        except httpx.RequestError as exc:
            raise ClickUpError(f"Could not reach ClickUp: {exc}") from exc

        if resp.status_code == 401:
            raise ClickUpError("ClickUp rejected the token. Regenerate it in Settings, Apps.")
        if resp.status_code == 404:
            raise ClickUpError(f"List {list_id} not found. Run: python -m radar.cli lists")
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("err", "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:120]
            raise ClickUpError(f"ClickUp said: {detail or resp.status_code}")

        data = resp.json()
        log.info("created ClickUp task %s in list %s", data.get("id"), list_id)
        return data["id"], data.get("url", "")

    # ---- setup helper ----

    def discover(self) -> list[dict]:
        """Every list you can write to, with the ids to paste into settings."""
        if not self.token:
            raise ClickUpError("CLICKUP_TOKEN is not set in .env")
        out: list[dict] = []

        def fetch(path: str, key: str) -> list[dict]:
            try:
                r = httpx.get(f"{API}/{path}", headers=self._headers(), timeout=30)
                r.raise_for_status()
                return r.json().get(key, [])
            except Exception as exc:  # noqa: BLE001
                log.debug("discover %s failed: %s", path, exc)
                return []

        for team in fetch("team", "teams"):
            for space in fetch(f"team/{team['id']}/space?archived=false", "spaces"):
                where = f"{team['name']} / {space['name']}"
                for lst in fetch(f"space/{space['id']}/list?archived=false", "lists"):
                    out.append({"id": lst["id"], "name": lst["name"], "path": where})
                for folder in fetch(f"space/{space['id']}/folder?archived=false", "folders"):
                    for lst in folder.get("lists", []):
                        out.append({"id": lst["id"], "name": lst["name"],
                                    "path": f"{where} / {folder['name']}"})
        return out

    def verify(self) -> str:
        """One-line health string for the dashboard footer."""
        if not self.token:
            return "ClickUp not connected — CLICKUP_TOKEN missing"
        if not self.default_list:
            return "ClickUp token set, but no list chosen"
        try:
            r = httpx.get(f"{API}/list/{self.default_list}",
                          headers=self._headers(), timeout=15)
            if r.status_code == 200:
                name = r.json().get("name", self.default_list)
                extra = f", {len(self.overrides)} override(s)" if self.overrides else ""
                return f"Tickets go to {name}{extra}"
            if r.status_code == 401:
                return "ClickUp token rejected"
            return f"ClickUp list {self.default_list} unreachable ({r.status_code})"
        except Exception as exc:  # noqa: BLE001
            return f"ClickUp unreachable: {exc}"

    # ---- formatting ----

    @staticmethod
    def _priority(importance: int) -> int:
        # ClickUp: 1 urgent, 2 high, 3 normal, 4 low
        return 1 if importance >= 85 else 2 if importance >= 70 else 3

    @staticmethod
    def _body(row: dict, entity_name: str) -> str:
        caption = row.get("caption") or "_No caption suggested — write one._"
        return "\n".join([
            f"**{entity_name}** · {row.get('category', 'update')} · "
            f"importance {row.get('importance', 0)}/100",
            "",
            row.get("summary", ""),
            "",
            "**Repost angle**",
            row.get("repost_angle") or "_Not specified._",
            "",
            "**Suggested caption**",
            f"> {caption}",
            "",
            "**Source**",
            f"{row.get('author', '')} · {(row.get('published_at') or '')[:10]}",
            row.get("url", ""),
            "",
            f"_Detected by GL Radar via {row.get('source', '')}._",
        ])

    def _custom_fields(self, row: dict, entity_name: str) -> list[dict]:
        mapping = self.cfg.get("custom_fields", {}) or {}
        values = {"source_url": row.get("url", ""), "company": entity_name,
                  "importance": row.get("importance", 0)}
        return [{"id": mapping[k], "value": v}
                for k, v in values.items() if mapping.get(k)]
