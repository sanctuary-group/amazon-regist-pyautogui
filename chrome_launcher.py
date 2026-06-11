from __future__ import annotations
import json
import os
import platform
import random
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Optional

import nodriver as uc

from config_loader import ChromeConfig
from utils import setup_logger

log = setup_logger("pco.chrome")


@dataclass
class ChromeHandle:
    """nodriver の Browser を抱えるハンドル。

    旧版の proc/port/ws_url 互換フィールドは廃止 (pipe 接続なので不要)。
    BrowserSession 側は handle.browser を参照する。
    """
    browser: Any  # nodriver.Browser


_BROWSER_CANDIDATES = {
    "chrome": {
        "Darwin": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        ],
        "Windows": [
            ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
            ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
            ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
        ],
        "Linux": [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ],
    },
    "edge": {
        "Darwin": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "Windows": [
            ("PROGRAMFILES(X86)", r"Microsoft\Edge\Application\msedge.exe"),
            ("PROGRAMFILES", r"Microsoft\Edge\Application\msedge.exe"),
        ],
        "Linux": ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"],
    },
    "brave": {
        "Darwin": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
        "Windows": [
            ("PROGRAMFILES", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            ("PROGRAMFILES(X86)", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            ("LOCALAPPDATA", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ],
        "Linux": ["/usr/bin/brave-browser", "/usr/bin/brave"],
    },
    "opera": {
        "Darwin": ["/Applications/Opera.app/Contents/MacOS/Opera"],
        "Windows": [
            ("LOCALAPPDATA", r"Programs\Opera\opera.exe"),
        ],
        "Linux": ["/usr/bin/opera"],
    },
    "vivaldi": {
        "Darwin": ["/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"],
        "Windows": [
            ("LOCALAPPDATA", r"Vivaldi\Application\vivaldi.exe"),
            ("PROGRAMFILES", r"Vivaldi\Application\vivaldi.exe"),
        ],
        "Linux": ["/usr/bin/vivaldi"],
    },
}


def _default_browser_candidates(browser_type: str) -> list[str]:
    system = platform.system()
    table = _BROWSER_CANDIDATES.get(browser_type)
    if table is None:
        log.warning(f"unknown browser_type={browser_type!r}, falling back to chrome candidates")
        table = _BROWSER_CANDIDATES["chrome"]
    entries = table.get(system, [])
    if system == "Windows":
        out: list[str] = []
        for env_key, rel in entries:
            base = os.environ.get(env_key, "")
            if not base:
                continue
            out.append(os.path.join(base, rel))
        return out
    return list(entries)


def _resolve_binary(configured: str, browser_type: str) -> str:
    if configured and os.path.exists(configured):
        return configured
    for c in _default_browser_candidates(browser_type):
        if c and os.path.exists(c):
            if configured and configured != c:
                log.warning(f"configured binary not found, falling back: {c}")
            return c
    # 見つからなくても起動は試みる（PATH 上にあるかもしれない）
    fallback = {
        "chrome": "google-chrome",
        "edge": "microsoft-edge",
        "brave": "brave-browser",
        "opera": "opera",
        "vivaldi": "vivaldi",
    }.get(browser_type, "google-chrome")
    return configured or fallback


def list_profiles(user_data_dir: str) -> dict[str, str]:
    """Chrome の Local State を読んで {profile_dir: display_name} を返す。"""
    ls_path = os.path.join(user_data_dir, "Local State")
    if not os.path.exists(ls_path):
        return {}
    try:
        with open(ls_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        info = (data.get("profile") or {}).get("info_cache") or {}
        return {k: (v.get("name") or k) for k, v in info.items()}
    except Exception as e:
        log.warning(f"Local State 読み込み失敗: {e}")
        return {}


_used_profiles: set[str] = set()


def reset_used_profiles() -> None:
    """同 run 中の「使用済み」記録をクリアする (テスト/再起動用)。"""
    _used_profiles.clear()


def pick_random_profile(
    user_data_dir: str, exclude: Optional[list[str]] = None,
) -> Optional[tuple[str, str]]:
    """利用可能なプロファイルからランダムに 1 つ返す (dir, display_name) のタプル。

    - exclude に含まれる dir 名はスキップ
    - 同 run 中に一度使用したプロファイルは除外
    - 全候補が使い切られたら自動でリセットして再選択
    - 候補そのものが無ければ None
    """
    profiles = list_profiles(user_data_dir)
    excl = set(exclude or [])
    base = [(d, n) for d, n in profiles.items() if d not in excl]
    if not base:
        return None
    fresh = [(d, n) for d, n in base if d not in _used_profiles]
    if not fresh:
        log.info(f"全 {len(base)} プロファイル使用済み → ローテーションをリセット")
        _used_profiles.clear()
        fresh = base
    picked = random.choice(fresh)
    _used_profiles.add(picked[0])
    log.info(
        f"profile pick: 候補={len(base)} 使用済み={len(_used_profiles)}/{len(base)} "
        f"未使用={len(fresh)} → 選択={picked[0]!r} ({picked[1]})"
    )
    return picked


def _disable_password_manager(user_data_dir: str) -> None:
    """Default/Preferences に「パスワードを保存しますか?」のバブルを抑制する設定を書く。

    - credentials_enable_service: パスワード保存提案の ON/OFF
    - profile.password_manager_enabled: 旧キー (互換)
    - credentials_enable_autosignin: 自動サインイン抑制

    既存 Preferences があれば該当キーだけ更新、なければ最小構成で新規作成する。
    automation 検知には影響しない (普通のユーザー設定と同じ JSON 値)。
    """
    # user_data_dir 配下の全プロファイル (Default + Profile N) に同じ設定を書き込む。
    # profile_rotation でどのプロファイルが使われても抑制が効くようにする。
    targets: list[str] = []
    try:
        for name in os.listdir(user_data_dir):
            sub = os.path.join(user_data_dir, name)
            if os.path.isdir(sub) and (name == "Default" or name.startswith("Profile ")):
                targets.append(sub)
    except FileNotFoundError:
        pass
    if not targets:
        # 何もなければ最低限 Default を作る (初回起動相当)
        d = os.path.join(user_data_dir, "Default")
        os.makedirs(d, exist_ok=True)
        targets.append(d)

    updated = 0
    for profile_dir in targets:
        prefs_path = os.path.join(profile_dir, "Preferences")
        prefs: dict = {}
        if os.path.exists(prefs_path):
            try:
                with open(prefs_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f) or {}
            except Exception as e:
                log.warning(f"既存 Preferences 読み込み失敗 ({profile_dir}): {e}")
                prefs = {}
        prefs["credentials_enable_service"] = False
        prefs["credentials_enable_autosignin"] = False
        profile = prefs.setdefault("profile", {})
        if not isinstance(profile, dict):
            profile = {}
            prefs["profile"] = profile
        profile["password_manager_enabled"] = False
        try:
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f, ensure_ascii=False)
            updated += 1
        except Exception as e:
            log.warning(f"Preferences 書き込み失敗 ({profile_dir}): {e}")
    log.debug(f"password manager 抑制: {updated}/{len(targets)} プロファイル更新")


def _clean_singleton_locks(user_data_dir: str) -> None:
    """stale な Singleton lock を毎回掃除する。

    Chrome は user_data_dir 内に SingletonLock / SingletonSocket / SingletonCookie を
    シンボリックリンクで作成し、既存インスタンスを検知する。
    前回 Chrome が異常終了 / pkill された場合これらが残り、次回起動時に
    「既に開いている」と誤判定されて --remote-debugging-port が無視される。
    """
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = os.path.join(user_data_dir, name)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
                log.debug(f"removed stale {name}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"failed to remove {name}: {e}")


def _clean_profile_locks(user_data_dir: str, profile_dir_name: str) -> None:
    """Profile X 配下の各種 LOCK / lockfile を掃除する。

    Chrome が異常終了するとプロファイル内に LOCK (leveldb) や lockfile が残り、
    次回そのプロファイルで起動した時に「他のプロセスが使用中」エラーになる。
    """
    profile_path = os.path.join(user_data_dir, profile_dir_name)
    if not os.path.isdir(profile_path):
        return
    targets = ["LOCK", "lockfile"]
    # 各種 sub-database 内の LOCK も掃除
    for sub in ("Local Storage", "Session Storage", "IndexedDB",
                "databases", "Service Worker", "WebStorage"):
        sub_path = os.path.join(profile_path, sub)
        if os.path.isdir(sub_path):
            for root, _, files in os.walk(sub_path):
                for f in files:
                    if f == "LOCK" or f == "lockfile":
                        targets.append(os.path.relpath(os.path.join(root, f), profile_path))
    removed = 0
    for rel in set(targets):
        p = os.path.join(profile_path, rel)
        try:
            if os.path.exists(p):
                os.unlink(p)
                removed += 1
        except Exception as e:
            log.debug(f"failed to remove {rel}: {e}")
    if removed:
        log.debug(f"profile lock cleanup: removed {removed} files from {profile_dir_name}")


async def launch(cfg: ChromeConfig) -> ChromeHandle:
    """nodriver で Chrome を起動 (--remote-debugging-pipe 経由)。

    旧 launch() の同期 subprocess.Popen 方式を廃止し、nodriver.start() に置換。
    --remote-debugging-port を使わないので PCO 側の bot 検知に引っかかりにくい。
    """
    os.makedirs(cfg.user_data_dir, exist_ok=True)
    _clean_singleton_locks(cfg.user_data_dir)
    _disable_password_manager(cfg.user_data_dir)

    binary = _resolve_binary(cfg.binary, getattr(cfg, "browser_type", "chrome"))

    # プロファイルローテーション (有効時のみ): user_data_dir 配下の Chrome プロファイルから
    # ランダムに 1 つ選んで --profile-directory に渡す。
    rotation_label = None
    extra = list(cfg.extra_args)
    extra = [a for a in extra if not a.startswith("--profile-directory")]
    active_profile = "Default"
    if getattr(cfg, "profile_rotation_enabled", False):
        picked = pick_random_profile(
            cfg.user_data_dir,
            exclude=getattr(cfg, "profile_rotation_exclude", []) or [],
        )
        if picked:
            pdir, pname = picked
            extra.append(f"--profile-directory={pdir}")
            rotation_label = f"{pdir} ({pname})"
            active_profile = pdir
        else:
            log.warning("profile rotation enabled だが候補なし → デフォルトで起動")
    else:
        # extra_args 内の --profile-directory= から active profile を拾う
        for a in extra:
            if a.startswith("--profile-directory="):
                active_profile = a.split("=", 1)[1] or "Default"
                break

    # 起動前にそのプロファイルのロック残骸を掃除
    _clean_profile_locks(cfg.user_data_dir, active_profile)

    # nodriver には --remote-debugging-port を渡さない (pipe 接続するため)
    browser_args = [
        f"--window-position={cfg.window_position[0]},{cfg.window_position[1]}",
        f"--window-size={cfg.window_size[0]},{cfg.window_size[1]}",
        "--no-first-run",
        "--no-default-browser-check",
        *extra,
    ]
    suffix = f" profile={rotation_label}" if rotation_label else ""
    log.info(
        f"launching {getattr(cfg, 'browser_type', 'chrome')} via nodriver "
        f"(user_data_dir={cfg.user_data_dir}){suffix}"
    )
    try:
        browser = await uc.start(
            headless=False,
            user_data_dir=cfg.user_data_dir,
            browser_executable_path=binary,
            browser_args=browser_args,
            sandbox=True,
        )
    except Exception as e:
        log.error(f"nodriver start 失敗: {e}")
        raise

    # 起動直後の安定化待機
    await _async_sleep(random.uniform(0.8, 1.6))
    log.info("nodriver ready")
    return ChromeHandle(browser=browser)


async def _async_sleep(sec: float) -> None:
    import asyncio
    await asyncio.sleep(sec)


def close(handle: ChromeHandle) -> None:
    """nodriver Browser を確実に終了させる。

    Browser.stop() はプロセス終了を待たずに返るので、続けて launch() すると
    user_data_dir 占有のレースになる。ここで process.wait() し、ダメなら kill。
    """
    proc = None
    try:
        proc = getattr(handle.browser, "_process", None)
        handle.browser.stop()
    except Exception as e:
        log.warning(f"chrome close error: {e}")

    if proc is None:
        return
    # asyncio.subprocess.Process なら poll() ではなく returncode を見る
    try:
        # nodriver の _process は asyncio.subprocess.Process
        import asyncio as _asyncio
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if proc.returncode is not None:
                return
            time.sleep(0.1)
        # まだ生きてたら kill
        if proc.returncode is None:
            log.warning(f"chrome process did not exit in 5s, killing pid={getattr(proc, 'pid', '?')}")
            try:
                proc.kill()
            except Exception:
                pass
            # 念のためもう少し待つ
            time.sleep(0.5)
    except Exception as e:
        log.debug(f"close: wait err (無視): {e}")
