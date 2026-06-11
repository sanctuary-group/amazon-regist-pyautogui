"""nodriver ベースの read-only CDP クライアント。

旧版は WebSocket 直叩き (--remote-debugging-port) だったが、PCO の bot 検知に引っかかるため
nodriver の --remote-debugging-pipe 経由 (stdio CDP) に移行。
公開 API (evaluate / navigate / current_url / get_element_rect / wait_for_selector / to_screen 等)
は維持しているので、apply_flow.py / guard.py は変更不要。
"""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

import nodriver as uc
from nodriver.cdp import browser as cdpb
from nodriver.cdp import runtime

from utils import setup_logger

if TYPE_CHECKING:
    from nodriver import Browser, Tab

log = setup_logger("pco.cdp")


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


class CDPReader:
    """Read-only CDP client (nodriver 経由)。入力イベントは発火しない。"""

    def __init__(self, browser: "Browser", tab: "Tab"):
        self._browser = browser
        self._tab = tab
        self._window_id: Optional[cdpb.WindowID] = None

    @classmethod
    async def from_browser(cls, browser: "Browser") -> "CDPReader":
        """Browser から最初の Tab を拾って構築。"""
        tab = browser.main_tab
        if tab is None:
            # 念のため update して取り直す
            await browser.update_targets()
            tab = browser.main_tab
            if tab is None:
                raise RuntimeError("no main_tab on browser")
        return cls(browser, tab)

    async def connect(self) -> None:
        """互換用 no-op (nodriver は uc.start() 時点で接続済み)。"""
        return None

    async def close(self) -> None:
        """互換用 no-op (ブラウザライフサイクルは BrowserSession 側が管理)。"""
        return None

    async def reconnect(self) -> None:
        """互換用: pipe 接続なのでこちらで再取得は不要。tab を最新で取り直すだけ。"""
        try:
            await self._browser.update_targets()
            t = self._browser.main_tab
            if t is not None:
                self._tab = t
                self._window_id = None
        except Exception as e:
            log.warning(f"reconnect: update_targets 失敗: {e}")

    async def navigate(self, url: str) -> None:
        await self._tab.get(url)

    async def current_url(self) -> str:
        v = await self._evaluate_raw("location.href")
        return v or ""

    async def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        return await self._evaluate_raw(expression, await_promise=await_promise)

    async def _evaluate_raw(
        self, expression: str, *, await_promise: bool = False,
    ) -> Any:
        """CDP の Runtime.evaluate を return_by_value=True で実行し、値を直接返す。"""
        try:
            result, exc = await self._tab.send(runtime.evaluate(
                expression=expression,
                return_by_value=True,
                await_promise=await_promise,
            ))
        except Exception as e:
            raise RuntimeError(f"evaluate failed: {e}")
        if exc is not None:
            text = getattr(exc, "text", None) or str(exc)
            raise RuntimeError(f"evaluate error: {text}")
        return result.value

    async def get_window_position(self) -> tuple[int, int, int, int]:
        """ウィンドウの (left, top, width, height) を screen 座標で返す。"""
        for attempt in range(2):
            try:
                if self._window_id is None:
                    wid, _ = await self._tab.send(cdpb.get_window_for_target())
                    self._window_id = wid
                bounds = await self._tab.send(cdpb.get_window_bounds(self._window_id))
                return (int(bounds.left), int(bounds.top), int(bounds.width), int(bounds.height))
            except Exception as e:
                msg = str(e).lower()
                if "window not found" in msg or "no window" in msg:
                    log.warning(f"window not found (attempt {attempt+1}), re-fetch window id")
                    self._window_id = None
                    continue
                raise
        raise RuntimeError("Browser window not found (after retry)")

    async def get_layout_metrics(self) -> dict:
        from nodriver.cdp import page as cdpp
        m = await self._tab.send(cdpp.get_layout_metrics())
        # dataclass to dict (互換のため)
        try:
            from dataclasses import asdict
            return asdict(m)
        except Exception:
            return {"_raw": str(m)}

    async def get_element_rect(self, selector: str, *, nth: int = 0) -> Optional[Rect]:
        expr = f"""
        (() => {{
          const list = document.querySelectorAll({json.dumps(selector)});
          const el = list[{nth}];
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return {{x: r.x, y: r.y, width: r.width, height: r.height,
                   visible: r.width > 0 && r.height > 0 && el.offsetParent !== null}};
        }})()
        """
        v = await self.evaluate(expr)
        if not v:
            return None
        if not v.get("visible"):
            return None
        return Rect(x=v["x"], y=v["y"], width=v["width"], height=v["height"])

    async def wait_for_selector(self, selector: str, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        expr = f"!!document.querySelector({json.dumps(selector)})"
        while time.time() < end:
            try:
                if await self.evaluate(expr):
                    return True
            except Exception as e:
                log.debug(f"wait_for_selector eval err: {e}")
            await asyncio.sleep(0.25)
        return False

    async def wait_for_selector_hidden(self, selector: str, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        expr = f"!document.querySelector({json.dumps(selector)})"
        while time.time() < end:
            try:
                if await self.evaluate(expr):
                    return True
            except Exception as e:
                log.debug(f"wait_for_selector_hidden eval err: {e}")
            await asyncio.sleep(0.25)
        return False

    async def to_screen(self, rect: Rect) -> Rect:
        """Viewport CSS-px → screen 座標 (論理ピクセル)。

        pyautogui は macOS / Windows いずれも論理ピクセル (= CSS px) を期待する。
        """
        result = await self.evaluate(
            "({ sx: window.screenX, sy: window.screenY,"
            "   toolbar: window.outerHeight - window.innerHeight,"
            "   dpr: window.devicePixelRatio })"
        )
        dpr = float(result["dpr"])  # ログ用
        sx = float(result["sx"])
        sy = float(result["sy"]) + float(result["toolbar"])
        log.debug(f"to_screen: dpr={dpr} viewport=({sx:.0f},{sy:.0f}) rect=({rect.x:.1f},{rect.y:.1f})")
        return Rect(
            x=sx + rect.x,
            y=sy + rect.y,
            width=rect.width,
            height=rect.height,
        )
