"""CAPTCHA / 403 / WAF 検出と、手動解決待ちのゲート。

検知されたら macOS 通知でユーザーに知らせ、解消されるまで polling で待つ。
タイムアウトで ABORT 扱い（呼び出し側がステータス記録して次へ）。
"""

from __future__ import annotations
import asyncio
import json
import platform
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from cdp_reader import CDPReader
from config_loader import GuardConfig
from utils import setup_logger

log = setup_logger("pco.guard")


class GuardTimeout(Exception):
    pass


def _as_applescript_quote(s: str) -> str:
    """AppleScript 文字列リテラル（\\n 非対応なのでスペースに置換）。"""
    s = s.replace("\r", " ").replace("\n", " — ")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _notify_macos(title: str, message: str, sound: Optional[str]) -> None:
    sound_clause = f" sound name {_as_applescript_quote(sound)}" if sound else ""
    script = (
        f"display notification {_as_applescript_quote(message)} "
        f"with title {_as_applescript_quote(title)}{sound_clause}"
    )
    subprocess.run(["osascript", "-e", script], check=False, timeout=5)


def _notify_windows(title: str, message: str, sound: Optional[str]) -> None:
    # System.Windows.Forms.NotifyIcon のバルーンチップ。追加パッケージ不要。
    ps_script = f"""
[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
[void][System.Reflection.Assembly]::LoadWithPartialName('System.Drawing')
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = {json.dumps(title)}
$n.BalloonTipText = {json.dumps(message)}
$n.Visible = $true
$n.ShowBalloonTip(10000)
Start-Sleep -Seconds 3
$n.Dispose()
""".strip()
    subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
        check=False, timeout=15,
    )


def _notify_linux(title: str, message: str, sound: Optional[str]) -> None:
    subprocess.run(["notify-send", title, message], check=False, timeout=5)


def notify(title: str, message: str, sound: Optional[str] = "Ping") -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            _notify_macos(title, message, sound)
        elif system == "Windows":
            _notify_windows(title, message, sound)
        else:
            _notify_linux(title, message, sound)
    except Exception as e:
        log.warning(f"notify failed ({system}): {e}")


@dataclass
class DetectionHit:
    reason: str
    detail: str


class Guard:
    def __init__(self, cdp: CDPReader, cfg: GuardConfig):
        self.cdp = cdp
        self.cfg = cfg

    async def _check_selectors(self) -> Optional[DetectionHit]:
        for sel in self.cfg.detection_selectors:
            try:
                expr = f"!!document.querySelector({json.dumps(sel)})"
                if await self.cdp.evaluate(expr):
                    return DetectionHit(reason="captcha_or_waf", detail=sel)
            except Exception:
                continue
        return None

    async def _check_url(self) -> Optional[DetectionHit]:
        try:
            url = await self.cdp.current_url()
        except Exception:
            return None
        for sub in self.cfg.error_url_substrings:
            if sub and sub in url:
                return DetectionHit(reason="error_url", detail=f"{sub} in {url}")
        return None

    async def check(self) -> Optional[DetectionHit]:
        hit = await self._check_url()
        if hit:
            return hit
        return await self._check_selectors()

    async def wait_clear(self, expected_selector: Optional[str] = None) -> None:
        """検知中の場合、ユーザーの手動解決で解消されるまで待つ。

        `cfg.manual_wait` が False なら待機せず即 GuardTimeout を raise。
        """
        hit = await self.check()
        if not hit:
            return
        if not self.cfg.manual_wait:
            log.warning(f"DETECTED: {hit.reason} ({hit.detail}) — 即スキップ (manual_wait=False)")
            raise GuardTimeout(f"captcha/waf detected and auto-skipped: {hit.reason} ({hit.detail})")
        log.warning(f"DETECTED: {hit.reason} ({hit.detail}) — 手動解決待ち")
        notify("PCO 自動応募", f"検知: {hit.reason}\n{hit.detail[:120]}", sound=self.cfg.notify_sound)

        deadline = time.time() + self.cfg.captcha_timeout_sec
        last_notify = time.time()
        while time.time() < deadline:
            await asyncio.sleep(5.0)
            still = await self.check()
            if still is None:
                # 期待セレクタが指定されていれば可視確認（フォールバック）
                if expected_selector:
                    ok = await self.cdp.wait_for_selector(expected_selector, timeout=3)
                    if not ok:
                        continue
                log.info("検知解消 → 再開")
                return
            # 60 秒ごとに通知リマインド
            if time.time() - last_notify > 60:
                notify("PCO 自動応募", "まだ手動解決待ちです", sound=None)
                last_notify = time.time()
        raise GuardTimeout(f"captcha/waf timeout after {self.cfg.captcha_timeout_sec}s")
