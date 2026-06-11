from __future__ import annotations
import os
import platform
import yaml
from dataclasses import dataclass, field
from pathlib import Path


SUPPORTED_BROWSERS = ("chrome", "edge", "brave", "opera", "vivaldi")


def _default_user_data_dir(browser_type: str) -> str:
    system = platform.system()
    name = f"amazon-bot-profile-{browser_type}"
    if system == "Darwin":
        return os.path.expanduser(f"~/Library/Application Support/{name}")
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, name)
    return os.path.expanduser(f"~/.config/{name}")


@dataclass
class GmailConfig:
    auth_email: str
    query: str
    timeout_sec: int
    credentials_file: str
    token_file: str


@dataclass
class ChromeConfig:
    binary: str
    user_data_dir: str
    debugging_port: int
    window_position: tuple[int, int]
    window_size: tuple[int, int]
    browser_type: str = "chrome"
    extra_args: list[str] = field(default_factory=list)
    profile_rotation_enabled: bool = False
    profile_rotation_exclude: list[str] = field(default_factory=list)


@dataclass
class MouseConfig:
    move_px_per_step_range: tuple[int, int]
    step_interval_ms: tuple[int, int]
    overshoot_probability: float
    target_jitter_px: int
    click_hold_ms: tuple[int, int]


@dataclass
class KeyboardConfig:
    key_down_ms: tuple[int, int]
    inter_key_ms: tuple[int, int]
    mistype_probability: float
    correction_pause_ms: tuple[int, int]


@dataclass
class ScrollConfig:
    tick_px_range: tuple[int, int]
    tick_interval_ms: tuple[int, int]
    readback_probability: float


@dataclass
class InputConfig:
    mouse: MouseConfig
    keyboard: KeyboardConfig
    scroll: ScrollConfig


@dataclass
class GuardConfig:
    captcha_timeout_sec: int
    notify_sound: str
    detection_selectors: list[str]
    error_url_substrings: list[str]
    manual_wait: bool = True


@dataclass
class RetriesConfig:
    login_403: int
    max_wait_sec_per_account: int
    max_wait_sec_apply: int


@dataclass
class UrlsConfig:
    register_start: str
    complete_substrings: list[str] = field(default_factory=list)


@dataclass
class NotificationsConfig:
    discord_webhook_url: str = ""
    on_critical: bool = True
    on_summary: bool = True
    mention: str = ""
    progress_every: int = 0


@dataclass
class WakeLockConfig:
    enabled: bool = True


@dataclass
class AutoResumeConfig:
    enabled: bool = False
    max_rounds: int = 0
    interval_sec: int = 1800
    give_up_if_no_progress: bool = True


@dataclass
class WifiSwitchConfig:
    enabled: bool = False
    interface: str = "en0"
    ssids: list[dict] = field(default_factory=list)  # [{ssid: str, password: str}, ...]
    connectivity_check_url: str = "https://www.google.com/generate_204"
    connectivity_timeout_sec: int = 30
    cooldown_sec: int = 10
    min_switch_interval_sec: int = 30
    switch_on_patterns: list[str] = field(default_factory=list)


@dataclass
class Config:
    gas_webapp_url: str
    sheet_name: str
    start_col_letter: str
    email_col_letter: str
    num_cols: int
    processing_order: str
    skip_statuses: list[str]
    gmail: GmailConfig
    chrome: ChromeConfig
    input: InputConfig
    guard: GuardConfig
    retries: RetriesConfig
    urls: UrlsConfig
    notifications: NotificationsConfig
    wake_lock: WakeLockConfig
    auto_resume: AutoResumeConfig
    wifi_switch: WifiSwitchConfig
    dryrun: bool


def _pair_int(v, default: tuple[int, int]) -> tuple[int, int]:
    if not v:
        return default
    return (int(v[0]), int(v[1]))


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        # 開発時/バンドル時で適切な場所を解決
        try:
            from paths import config_path
            path = config_path()
        except ImportError:
            path = "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    ch = raw.get("chrome", {})
    browser_type = str(ch.get("browser_type", "chrome") or "chrome").lower()
    if browser_type not in SUPPORTED_BROWSERS:
        raise ValueError(
            f"unsupported browser_type: {browser_type!r} (supported: {', '.join(SUPPORTED_BROWSERS)})"
        )
    raw_user_data_dir = ch.get("user_data_dir") or ""
    if raw_user_data_dir:
        user_data_dir = os.path.expandvars(os.path.expanduser(raw_user_data_dir))
    else:
        user_data_dir = _default_user_data_dir(browser_type)
    rotation_raw = ch.get("profile_rotation") or {}
    chrome = ChromeConfig(
        binary=ch.get("binary") or "",
        user_data_dir=user_data_dir,
        debugging_port=int(ch.get("debugging_port", 0)),
        window_position=_pair_int(ch.get("window_position"), (100, 80)),
        window_size=_pair_int(ch.get("window_size"), (1280, 900)),
        browser_type=browser_type,
        extra_args=list(ch.get("extra_args") or []),
        profile_rotation_enabled=bool(rotation_raw.get("enabled", False)),
        profile_rotation_exclude=list(rotation_raw.get("exclude") or []),
    )

    inp = raw.get("input", {})
    mouse_raw = inp.get("mouse", {})
    kb_raw = inp.get("keyboard", {})
    sc_raw = inp.get("scroll", {})
    input_cfg = InputConfig(
        mouse=MouseConfig(
            move_px_per_step_range=_pair_int(mouse_raw.get("move_px_per_step_range"), (3, 6)),
            step_interval_ms=_pair_int(mouse_raw.get("step_interval_ms"), (7, 14)),
            overshoot_probability=float(mouse_raw.get("overshoot_probability", 0.25)),
            target_jitter_px=int(mouse_raw.get("target_jitter_px", 2)),
            click_hold_ms=_pair_int(mouse_raw.get("click_hold_ms"), (40, 120)),
        ),
        keyboard=KeyboardConfig(
            key_down_ms=_pair_int(kb_raw.get("key_down_ms"), (30, 90)),
            inter_key_ms=_pair_int(kb_raw.get("inter_key_ms"), (50, 220)),
            mistype_probability=float(kb_raw.get("mistype_probability", 0.03)),
            correction_pause_ms=_pair_int(kb_raw.get("correction_pause_ms"), (150, 400)),
        ),
        scroll=ScrollConfig(
            tick_px_range=_pair_int(sc_raw.get("tick_px_range"), (3, 9)),
            tick_interval_ms=_pair_int(sc_raw.get("tick_interval_ms"), (40, 140)),
            readback_probability=float(sc_raw.get("readback_probability", 0.08)),
        ),
    )

    g = raw.get("guard", {})
    guard = GuardConfig(
        captcha_timeout_sec=int(g.get("captcha_timeout_sec", 300)),
        notify_sound=g.get("notify_sound", "Ping"),
        detection_selectors=list(g.get("detection_selectors") or []),
        error_url_substrings=list(g.get("error_url_substrings") or []),
        manual_wait=bool(g.get("manual_wait", True)),
    )

    return Config(
        gas_webapp_url=raw["gas_webapp_url"],
        sheet_name=raw.get("sheet_name", "会員登録"),
        start_col_letter=raw.get("start_col_letter", "A"),
        email_col_letter=raw.get("email_col_letter", "A"),
        num_cols=int(raw.get("num_cols", 4)),
        processing_order=raw.get("processing_order", "top"),
        skip_statuses=list(raw.get("skip_statuses") or []),
        gmail=_load_gmail(raw["gmail"]),
        chrome=chrome,
        input=input_cfg,
        guard=guard,
        retries=RetriesConfig(**raw["retries"]),
        urls=_load_urls(raw.get("urls") or {}),
        notifications=_load_notifications(raw.get("notifications") or {}),
        wake_lock=_load_wake_lock(raw.get("wake_lock") or {}),
        auto_resume=_load_auto_resume(raw.get("auto_resume") or {}),
        wifi_switch=_load_wifi_switch(raw.get("wifi_switch") or {}),
        dryrun=bool((raw.get("dryrun") or {}).get("enabled", False)),
    )


def _load_gmail(g: dict) -> GmailConfig:
    """credentials_file / token_file が相対パスなら app_data_dir で解決する。"""
    cred = g.get("credentials_file", "credentials.json")
    tok = g.get("token_file", "token.json")
    if not os.path.isabs(cred):
        try:
            from paths import app_data_dir
            cred = str(app_data_dir() / cred)
        except ImportError:
            pass
    if not os.path.isabs(tok):
        try:
            from paths import app_data_dir
            tok = str(app_data_dir() / tok)
        except ImportError:
            pass
    return GmailConfig(
        auth_email=g.get("auth_email", ""),
        query=g["query"],
        timeout_sec=int(g.get("timeout_sec", 120)),
        credentials_file=cred,
        token_file=tok,
    )


def _load_urls(u: dict) -> UrlsConfig:
    return UrlsConfig(
        register_start=str(
            u.get("register_start") or "https://www.amazon.co.jp/ap/register"
        ),
        complete_substrings=list(
            u.get("complete_substrings")
            or ["/gp/yourstore", "/gp/css/homepage", "/ref=nav_ya_signin"]
        ),
    )


def _load_notifications(n: dict) -> NotificationsConfig:
    return NotificationsConfig(
        discord_webhook_url=str(n.get("discord_webhook_url") or ""),
        on_critical=bool(n.get("on_critical", True)),
        on_summary=bool(n.get("on_summary", True)),
        mention=str(n.get("mention") or ""),
        progress_every=int(n.get("progress_every", 0) or 0),
    )


def _load_wake_lock(w: dict) -> WakeLockConfig:
    return WakeLockConfig(enabled=bool(w.get("enabled", True)))


def _load_wifi_switch(w: dict) -> WifiSwitchConfig:
    raw_ssids = w.get("ssids") or []
    ssids: list[dict] = []
    for entry in raw_ssids:
        if isinstance(entry, dict):
            ssids.append({
                "ssid": str(entry.get("ssid") or ""),
                "password": str(entry.get("password") or ""),
            })
    return WifiSwitchConfig(
        enabled=bool(w.get("enabled", False)),
        interface=str(w.get("interface") or "en0"),
        ssids=ssids,
        connectivity_check_url=str(
            w.get("connectivity_check_url") or "https://www.google.com/generate_204"
        ),
        connectivity_timeout_sec=int(w.get("connectivity_timeout_sec", 30) or 30),
        cooldown_sec=int(w.get("cooldown_sec", 10) or 10),
        min_switch_interval_sec=int(w.get("min_switch_interval_sec", 30) or 30),
        switch_on_patterns=list(w.get("switch_on_patterns") or []),
    )


def _load_auto_resume(a: dict) -> AutoResumeConfig:
    return AutoResumeConfig(
        enabled=bool(a.get("enabled", False)),
        max_rounds=int(a.get("max_rounds", 0) or 0),
        interval_sec=int(a.get("interval_sec", 1800) or 1800),
        give_up_if_no_progress=bool(a.get("give_up_if_no_progress", True)),
    )
