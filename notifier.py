from __future__ import annotations
from typing import Mapping

import requests

from config_loader import NotificationsConfig
from utils import setup_logger

log = setup_logger("pco.notify")

_DISCORD_MSG_LIMIT = 1900


class DiscordNotifier:
    def __init__(self, cfg: NotificationsConfig):
        self.cfg = cfg

    def critical(self, title: str, detail: str) -> None:
        if not self.cfg.on_critical:
            return
        self._post(f"🔴 **{title}**\n```\n{self._truncate(detail)}\n```", mention=True)

    def summary(
        self, total: int, success: int, failed: int, breakdown: Mapping[str, int],
    ) -> None:
        if not self.cfg.on_summary:
            return
        lines = [
            "📊 **実行終了サマリ**",
            f"対象 {total} 件 / 成功 {success} / 失敗 {failed}",
        ]
        if breakdown:
            lines.append("")
            lines.append("**内訳**")
            for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {k}: {v}")
        self._post("\n".join(lines), mention=False)

    def progress(
        self, done: int, total: int, breakdown: Mapping[str, int],
    ) -> None:
        if self.cfg.progress_every <= 0:
            return
        lines = [f"⏳ **進捗 {done}/{total}**"]
        if breakdown:
            parts = [f"{k} {v}" for k, v in sorted(breakdown.items(), key=lambda kv: -kv[1])]
            lines.append(" / ".join(parts))
        self._post("\n".join(lines), mention=False)

    def _post(self, content: str, *, mention: bool) -> None:
        url = self.cfg.discord_webhook_url
        if not url:
            return
        body = content
        if mention and self.cfg.mention:
            body = f"{self.cfg.mention} {body}"
        try:
            r = requests.post(
                url,
                json={"content": self._truncate(body, _DISCORD_MSG_LIMIT)},
                timeout=5,
            )
            if r.status_code >= 300:
                log.warning(f"discord notify non-2xx: {r.status_code} {r.text[:200]}")
        except Exception as e:
            log.warning(f"discord notify failed: {e}")

    @staticmethod
    def _truncate(s: str, limit: int = 1500) -> str:
        if len(s) <= limit:
            return s
        return s[: limit - 20] + "\n... (truncated)"
