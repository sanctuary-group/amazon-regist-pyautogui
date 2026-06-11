"""クロスプラットフォームの OS レベル入力（pyautogui + macOS Unicode 直接注入）。

マウス/スクロール/特殊キーは pyautogui（物理入力相当のイベント）を使用:
- macOS:  Quartz CGEventPost on kCGHIDEventTap
- Windows: Win32 SendInput

文字入力は IME の影響を避けるため macOS では Quartz の
CGEventKeyboardSetUnicodeString を直接使って IME をバイパスする。
Windows は pyautogui.keyDown/keyUp で ASCII をそのまま送信（日本語 IME の
影響を受けるケースは少ないが、必要なら将来 WM_CHAR 直送も検討）。

いずれも JS からは event.isTrusted === true に見える。
"""

from __future__ import annotations
import asyncio
import ctypes
import ctypes.wintypes
import math
import platform
import random
import time

import pyautogui

from config_loader import InputConfig
from utils import setup_logger

log = setup_logger("pco.input")

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# Windows SendInput 用定数・構造体
if IS_WINDOWS:
    _INPUT_KEYBOARD = 1
    _KEYEVENTF_UNICODE = 0x0004
    _KEYEVENTF_KEYUP = 0x0002

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.wintypes.WORD),
            ("wScan", ctypes.wintypes.WORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR: 8 bytes on 64-bit
        ]

    class _INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            # Union must be >= MOUSEINPUT (28 bytes on 64-bit); pad to ensure correct size
            _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 28)]
        _anonymous_ = ["_u"]
        _fields_ = [("type", ctypes.wintypes.DWORD), ("_u", _U)]

    def _send_unicode_char(ch: str, keyup: bool = False) -> None:
        """キーボードレイアウトに依存せず Unicode 文字を SendInput で送信する。"""
        flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if keyup else 0)
        inp = _INPUT(type=_INPUT_KEYBOARD)
        inp.ki = _KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=0)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

if IS_MACOS:
    # pyautogui が入っていれば pyobjc も一緒に入るので import は通る
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventPost,
        kCGHIDEventTap,
    )

# pyautogui のデフォルト安全装置を無効化
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

# pyautogui 用の特殊文字 → キー名マッピング（Windows / Linux 用）
_KEYMAP = {
    "\b": "backspace",
    "\n": "enter",
    "\r": "enter",
    "\t": "tab",
}

# macOS 用の virtual key code（IME を使わない特殊キー）
_KC_MAC = {
    "\b": 51,   # Delete (Backspace)
    "\n": 36,   # Return
    "\r": 36,
    "\t": 48,   # Tab
}


class HumanInput:
    def __init__(self, cfg: InputConfig, *, dryrun: bool = False):
        self.cfg = cfg
        self.dryrun = dryrun

    # ----- mouse primitives -----
    def current_pos(self) -> tuple[float, float]:
        x, y = pyautogui.position()
        return (float(x), float(y))

    def _post_move(self, x: float, y: float) -> None:
        pyautogui.moveTo(x, y, duration=0, _pause=False)

    def _post_button(self, down: bool, x: float, y: float) -> None:
        # 呼び出し前に _post_move で (x, y) に居る前提。ここでは warp しない
        if down:
            pyautogui.mouseDown(button="left", _pause=False)
        else:
            pyautogui.mouseUp(button="left", _pause=False)

    @staticmethod
    def _ease(t: float) -> float:
        return 0.5 - 0.5 * math.cos(math.pi * t)

    @staticmethod
    def _bezier(p0, p1, p2, t):
        u = 1 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        return x, y

    def _glide(self, src, dst) -> int:
        mc = self.cfg.mouse
        dist = math.hypot(dst[0] - src[0], dst[1] - src[1])
        px_lo, px_hi = mc.move_px_per_step_range
        step_px = random.uniform(px_lo, px_hi)
        steps = max(20, int(dist / max(step_px, 1.0)))
        mid = ((src[0] + dst[0]) / 2, (src[1] + dst[1]) / 2)
        ctrl = (
            mid[0] + random.uniform(-dist * 0.25, dist * 0.25),
            mid[1] + random.uniform(-dist * 0.15, dist * 0.15),
        )
        ivl_lo, ivl_hi = mc.step_interval_ms
        for i in range(1, steps + 1):
            t = self._ease(i / steps)
            x, y = self._bezier(src, ctrl, dst, t)
            if not self.dryrun:
                self._post_move(x, y)
            time.sleep(random.uniform(ivl_lo, ivl_hi) / 1000.0)
        return steps

    def move_to(self, x: float, y: float) -> dict:
        mc = self.cfg.mouse
        src = self.current_pos()
        jitter = mc.target_jitter_px
        target = (
            x + random.uniform(-jitter, jitter),
            y + random.uniform(-jitter, jitter),
        )
        dist = math.hypot(target[0] - src[0], target[1] - src[1])
        steps = 0
        overshot = False
        if dist > 150 and random.random() < mc.overshoot_probability:
            overshot = True
            sgn_x = 1 if target[0] >= src[0] else -1
            sgn_y = 1 if target[1] >= src[1] else -1
            over = (
                target[0] + random.uniform(20, 60) * sgn_x,
                target[1] + random.uniform(10, 40) * sgn_y,
            )
            steps += self._glide(src, over)
            time.sleep(random.uniform(0.04, 0.12))
            steps += self._glide(over, target)
        else:
            steps += self._glide(src, target)
        return {"steps": steps, "overshoot": overshot, "dist": dist}

    def click(self, x: float, y: float, *, label: str = "") -> dict:
        mc = self.cfg.mouse
        info = self.move_to(x, y)
        time.sleep(random.uniform(0.05, 0.18))
        hold_lo, hold_hi = mc.click_hold_ms
        if self.dryrun:
            log.info(f"[dryrun] click({x:.1f},{y:.1f}) label={label} steps={info['steps']} over={info['overshoot']}")
            return info
        self._post_button(True, x, y)
        time.sleep(random.uniform(hold_lo, hold_hi) / 1000.0)
        self._post_button(False, x, y)
        log.info(f"click({x:.1f},{y:.1f}) label={label} steps={info['steps']} over={info['overshoot']}")
        return info

    # ----- keyboard -----
    def _post_key(self, ch: str, down: bool) -> None:
        if IS_MACOS:
            # IME をバイパスするため Quartz 直接注入（日本語 IME が ON でも影響なし）
            kc = _KC_MAC.get(ch)
            if kc is not None:
                ev = CGEventCreateKeyboardEvent(None, kc, down)
            else:
                ev = CGEventCreateKeyboardEvent(None, 0, down)
                CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            CGEventPost(kCGHIDEventTap, ev)
            return

        # Windows: 特殊キーは pyautogui、通常文字は SendInput KEYEVENTF_UNICODE
        if IS_WINDOWS and ch not in _KEYMAP:
            _send_unicode_char(ch, keyup=not down)
            return

        key = _KEYMAP.get(ch, ch)
        try:
            if down:
                pyautogui.keyDown(key, _pause=False)
            else:
                pyautogui.keyUp(key, _pause=False)
        except Exception:
            if down:
                try:
                    pyautogui.typewrite(ch, interval=0, _pause=False)
                except Exception as e:
                    log.warning(f"typewrite failed for {ch!r}: {e}")

    def _press(self, ch: str) -> None:
        kc = self.cfg.keyboard
        down_lo, down_hi = kc.key_down_ms
        if self.dryrun:
            return
        self._post_key(ch, True)
        time.sleep(random.uniform(down_lo, down_hi) / 1000.0)
        self._post_key(ch, False)

    def type_text(self, s: str, *, label: str = "") -> dict:
        kc = self.cfg.keyboard
        inter_lo, inter_hi = kc.inter_key_ms
        cor_lo, cor_hi = kc.correction_pause_ms
        mistakes = 0
        for ch in s:
            self._press(ch)
            time.sleep(random.uniform(inter_lo, inter_hi) / 1000.0)
            if random.random() < kc.mistype_probability and ch.isalpha():
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                self._press(wrong)
                mistakes += 1
                time.sleep(random.uniform(cor_lo, cor_hi) / 1000.0)
                self._press("\b")
                time.sleep(random.uniform(inter_lo, inter_hi) / 1000.0)
        if self.dryrun:
            log.info(f"[dryrun] type({len(s)} chars) label={label}")
        else:
            log.info(f"type({len(s)} chars) label={label} mistakes={mistakes}")
        return {"chars": len(s), "mistakes": mistakes}

    def press_key(self, ch: str) -> None:
        if self.dryrun:
            log.info(f"[dryrun] press_key({ch!r})")
            return
        self._press(ch)

    # ----- scroll -----
    def _post_scroll(self, delta_px: int) -> None:
        # pyautogui.scroll は "clicks" 単位（macOS では line、Windows では 120=1notch）
        # およそ 1 click ≒ 30px 相当として換算
        clicks = max(1, int(abs(delta_px) / 30))
        direction = 1 if delta_px > 0 else -1
        pyautogui.scroll(direction * clicks, _pause=False)

    def scroll(self, total_px: int, *, down: bool = True) -> dict:
        sc = self.cfg.scroll
        tick_lo, tick_hi = sc.tick_px_range
        ivl_lo, ivl_hi = sc.tick_interval_ms
        remaining = abs(int(total_px))
        direction = -1 if down else 1  # 負値=下スクロール（pyautogui 準拠）
        ticks = 0
        readbacks = 0
        while remaining > 0:
            step = random.randint(tick_lo, tick_hi)
            delta = direction * step
            if not self.dryrun:
                self._post_scroll(delta)
            ticks += 1
            time.sleep(random.uniform(ivl_lo, ivl_hi) / 1000.0)
            remaining -= step
            if random.random() < sc.readback_probability and remaining > 50:
                readbacks += 1
                for _ in range(random.randint(2, 5)):
                    back = -direction * random.randint(tick_lo, tick_hi)
                    if not self.dryrun:
                        self._post_scroll(back)
                    time.sleep(random.uniform(ivl_lo, ivl_hi) / 1000.0)
        log.info(f"{'[dryrun] ' if self.dryrun else ''}scroll(total={total_px}, down={down}) ticks={ticks} readbacks={readbacks}")
        return {"ticks": ticks, "readbacks": readbacks}


async def async_sleep(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))
