"""Amazon 会員登録フロー: CDP で DOM を読み、OS レベル（Quartz）で実入力する。

amazon-regist-bot/content.js の 8 ページ状態機械を Python へ移植したもの。
ブラウザの Input ドメインは経由せず、human_input（pyautogui/Quartz）で実入力する。

ページ種別（content.js getCurrentPage 準拠）:
  EMAIL_INPUT    /ap/register      メール入力
  INTENT_CONFIRM /ax/claim/intent  インテント確認
  PASSWORD_INPUT /ap/register      氏名 + パスワード
  CAPTCHA        /ap/cvf/request   ARKOSE 画像認証（手動）
  EMAIL_OTP      /ap/cvf/request   メール OTP（Gmail 自動取得）
  PHONE_INPUT    /ap/cvf/verify    電話番号登録
  SMS_OTP        /ap/cvf/verify    SMS OTP（手動）
  COMPLETE       /gp/yourstore 等   完了
  ERROR / UNKNOWN
"""

from __future__ import annotations
import asyncio
import json
import random
import time
from typing import Optional

from cdp_reader import CDPReader, Rect
from guard import Guard, GuardTimeout, notify
from gmail_client import GmailClient
from human_input import HumanInput
from utils import human_delay, now_jst_str, setup_logger

log = setup_logger("amazon.flow")

# ===== ページ定数 =====
EMAIL_INPUT = "EMAIL_INPUT"
INTENT_CONFIRM = "INTENT_CONFIRM"
PASSWORD_INPUT = "PASSWORD_INPUT"
CAPTCHA = "CAPTCHA"
EMAIL_OTP = "EMAIL_OTP"
PHONE_INPUT = "PHONE_INPUT"
SMS_OTP = "SMS_OTP"
COMPLETE = "COMPLETE"
ERROR = "ERROR"
UNKNOWN = "UNKNOWN"

# content.js のフォールバック順をそのまま使用（優先度順）
SEL_EMAIL = ['#ap_email_login', '#ap_email', '[name="email"]', 'input[type="email"]']
SEL_EMAIL_CONTINUE = ['#continue', 'input#continue', 'input[type="submit"]', '.a-button-primary input']
SEL_INTENT_SUBMIT = [
    '#intent-confirmation-form input[type="submit"]',
    'input[aria-labelledby="intention-submit-button-announce"]',
    'form#intent-confirmation-form input[type="submit"]',
    '.a-button-primary input',
]
SEL_NAME = ['#ap_customer_name', '[name="customerName"]']
SEL_PASSWORD = ['#ap_password', '[name="password"]', 'input[type="password"]']
SEL_PASSWORD_CHECK = ['#ap_password_check', '[name="passwordCheck"]']
SEL_PASSWORD_SUBMIT = [
    '#continue', 'input#continue',
    'input[aria-labelledby="auth-continue-announce"]',
    'input[type="submit"]', '.a-button-primary input',
]
SEL_OTP_CODE = ['#cvf-input-code', '[name="code"]', '[name="cvf-input-code"]']
SEL_PHONE_CC = ['[name="cvf_phone_cc"]']
SEL_PHONE_NUM = ['#cvfPhoneNumber', '[name="cvf_phone_num"]']
SEL_PHONE_SUBMIT = [
    'input[type="submit"][value="collect"]',
    '.a-button-primary input[type="submit"]',
    '.a-button-primary input',
    'input[type="submit"]',
]

MARK = "data-az"


# ===== 低レベルプリミティブ（apply_flow.py 由来） =====

async def _settle() -> None:
    await asyncio.sleep(random.uniform(0.4, 0.9))


async def _screen_rect(cdp: CDPReader, selector: str, *, nth: int = 0) -> Optional[Rect]:
    rect = await cdp.get_element_rect(selector, nth=nth)
    if not rect:
        return None
    return await cdp.to_screen(rect)


async def _mark_first(cdp: CDPReader, selectors: list[str]) -> Optional[str]:
    """selectors を優先度順に探索し、最初に見つかった要素へ MARK 属性を付ける。

    可視（offsetParent あり）を優先し、なければ存在する最初の要素にフォールバック。
    マッチしたセレクタ文字列を返す（無ければ None）。
    """
    expr = f"""
    (() => {{
      const sels = {json.dumps(selectors)};
      const vis = (el) => el && el.offsetParent !== null
        && el.getBoundingClientRect().width > 0
        && el.getBoundingClientRect().height > 0;
      const apply = (el) => {{ el.setAttribute('{MARK}','1');
        el.scrollIntoView({{block:'center', behavior:'instant'}}); }};
      for (const s of sels) {{
        let el; try {{ el = document.querySelector(s); }} catch(e) {{ continue; }}
        if (vis(el)) {{ apply(el); return s; }}
      }}
      for (const s of sels) {{
        let el; try {{ el = document.querySelector(s); }} catch(e) {{ continue; }}
        if (el) {{ apply(el); return s; }}
      }}
      return null;
    }})()
    """
    return await cdp.evaluate(expr)


async def _unmark(cdp: CDPReader) -> None:
    await cdp.evaluate(
        f"(() => {{ document.querySelectorAll('[{MARK}]')"
        f".forEach(e => e.removeAttribute('{MARK}')); }})()"
    )


async def _click_marked(cdp: CDPReader, hid: HumanInput, *, label: str = "") -> bool:
    sel = f'[{MARK}="1"]'
    if not await cdp.wait_for_selector(sel, timeout=5):
        return False
    await _settle()
    rect = await _screen_rect(cdp, sel)
    if not rect:
        log.warning(f"no rect: {label}")
        return False
    cx, cy = rect.center
    hid.click(cx, cy, label=label or sel)
    return True


async def click_any(cdp: CDPReader, hid: HumanInput, selectors: list[str], *, label: str = "") -> bool:
    """selectors を優先度順に探索して最初の要素を OS クリックする。"""
    matched = await _mark_first(cdp, selectors)
    if not matched:
        log.warning(f"click_any: not found ({label}): {selectors}")
        return False
    log.info(f"click_any: {label} → {matched}")
    ok = await _click_marked(cdp, hid, label=label)
    await _unmark(cdp)
    return ok


async def type_any(cdp: CDPReader, hid: HumanInput, selectors: list[str], text: str, *, label: str = "") -> bool:
    """selectors を優先度順に探索し、最初の入力欄へ click → type。"""
    matched = await _mark_first(cdp, selectors)
    if not matched:
        log.warning(f"type_any: not found ({label}): {selectors}")
        return False
    sel = f'[{MARK}="1"]'
    # 既存値（autofill/password manager）を JS で消す
    await cdp.evaluate(f"""
    (() => {{
      const el = document.querySelector({json.dumps(sel)});
      if (!el) return;
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')?.set;
      if (setter) setter.call(el, ''); else el.value = '';
      el.dispatchEvent(new Event('input', {{bubbles:true}}));
      el.dispatchEvent(new Event('change', {{bubbles:true}}));
    }})()
    """)
    await human_delay(0.1, 0.25)
    if not await _click_marked(cdp, hid, label=f"{label}:focus"):
        await _unmark(cdp)
        return False
    await human_delay(0.25, 0.6)
    hid.type_text(text, label=label)
    await _unmark(cdp)
    return True


async def set_select_value(cdp: CDPReader, selectors: list[str], value: str, *, label: str = "") -> bool:
    """<select> の値を JS で set し change を発火する（OS 入力では選べないため）。"""
    matched = await _mark_first(cdp, selectors)
    if not matched:
        log.warning(f"set_select_value: not found ({label}): {selectors}")
        return False
    sel = f'[{MARK}="1"]'
    ok = await cdp.evaluate(f"""
    (() => {{
      const el = document.querySelector({json.dumps(sel)});
      if (!el) return false;
      el.value = {json.dumps(value)};
      el.dispatchEvent(new Event('change', {{bubbles:true}}));
      return true;
    }})()
    """)
    await _unmark(cdp)
    return bool(ok)


# ===== ページ判定（content.js getCurrentPage 準拠） =====

async def detect_page(cdp: CDPReader) -> str:
    expr = """
    (() => {
      const path = location.pathname;
      const q = (s) => { try { return document.querySelector(s); } catch(e){ return null; } };
      if (path.includes('/gp/yourstore') || path.includes('/gp/css/homepage')
          || path.includes('/ref=nav_ya_signin')) return 'COMPLETE';
      if (path.includes('/ap/cvf')) {
        if (q('#cvf-aamation-challenge-iframe')) return 'CAPTCHA';
        if (q('#cvfPhoneNumber') || q('[name="cvf_phone_num"]')) return 'PHONE_INPUT';
        if (q('#cvf-input-code') || q('[name="code"]')) {
          if (path.includes('/ap/cvf/verify')) return 'SMS_OTP';
          return 'EMAIL_OTP';
        }
      }
      if (path.includes('/ax/claim/intent')) return 'INTENT_CONFIRM';
      if (path.includes('/ap/register')) {
        if (q('#ap_password') || q('#ap_password_check') || q('[name="passwordCheck"]'))
          return 'PASSWORD_INPUT';
        if (q('#ap_email_login') || q('#ap_email') || q('input[type="email"]'))
          return 'EMAIL_INPUT';
      }
      if (q('#nav-link-accountList[data-nav-ref="nav_youraccount_btn"]')) return 'COMPLETE';
      if (q('.a-alert-error') || q('#auth-error-message-box')) return 'ERROR';
      return 'UNKNOWN';
    })()
    """
    try:
        return str(await cdp.evaluate(expr) or UNKNOWN)
    except Exception as e:
        log.warning(f"detect_page error: {e}")
        return UNKNOWN


async def _error_message(cdp: CDPReader) -> str:
    try:
        msg = await cdp.evaluate(
            "(() => { const b = document.querySelector('.a-alert-error')"
            " || document.querySelector('#auth-error-message-box');"
            " return b ? (b.textContent||'').trim().slice(0,100) : '不明なエラー'; })()"
        )
        return str(msg or "不明なエラー")
    except Exception:
        return "不明なエラー"


# ===== ページ別ハンドラ =====

async def handle_email_input(cdp: CDPReader, hid: HumanInput, account) -> None:
    log.info(f"page: メール入力 {account.email}")
    await human_delay(0.4, 0.8)
    if not await type_any(cdp, hid, SEL_EMAIL, account.email, label="email"):
        log.error("メール入力欄が見つかりません")
        return
    await human_delay(0.4, 0.8)
    await click_any(cdp, hid, SEL_EMAIL_CONTINUE, label="email_continue")


async def handle_intent_confirm(cdp: CDPReader, hid: HumanInput) -> None:
    log.info("page: インテント確認")
    await human_delay(0.4, 0.8)
    if not await click_any(cdp, hid, SEL_INTENT_SUBMIT, label="intent_submit"):
        log.warning("インテント確認ボタン未検出 → form.submit フォールバック")
        await cdp.evaluate(
            "(() => { const f = document.querySelector('#intent-confirmation-form')"
            " || document.querySelector('form'); if (f) f.submit(); })()"
        )


async def handle_password_input(cdp: CDPReader, hid: HumanInput, account) -> None:
    log.info(f"page: 氏名+パスワード入力 {account.email}")
    await human_delay(0.4, 0.8)
    if not await type_any(cdp, hid, SEL_NAME, account.name, label="name"):
        log.warning("氏名フィールドが見つかりません")
    await human_delay(0.2, 0.4)
    if not await type_any(cdp, hid, SEL_PASSWORD, account.password, label="password"):
        log.warning("パスワードフィールドが見つかりません")
    await human_delay(0.2, 0.4)
    if not await type_any(cdp, hid, SEL_PASSWORD_CHECK, account.password, label="password_check"):
        log.warning("パスワード確認フィールドが見つかりません")
    await human_delay(0.4, 0.8)
    if not await click_any(cdp, hid, SEL_PASSWORD_SUBMIT, label="password_submit"):
        log.warning("登録ボタン未検出 → form.submit フォールバック")
        await cdp.evaluate(
            "(() => { const f = document.querySelector('#ap_register_form')"
            " || document.querySelector('form'); if (f) f.submit(); })()"
        )


async def handle_email_otp(cdp: CDPReader, hid: HumanInput, gmail: GmailClient, account, *, notify_sound: str) -> None:
    log.info(f"page: メール OTP（Gmail 取得待機） {account.email}")
    await human_delay(0.4, 0.8)
    since_ms = int(time.time() * 1000)
    code = await gmail.wait_otp(login_email=account.email, since_ms=since_ms)
    if not code:
        log.error("メール OTP 取得失敗")
        notify("Amazon 登録", f"{account.email}: メール OTP を自動取得できません。手動入力してください", sound=notify_sound)
        return
    log.info(f"メール OTP 取得: {code}")
    if not await type_any(cdp, hid, SEL_OTP_CODE, code, label="email_otp"):
        log.error("OTP 入力欄が見つかりません")
        return
    # 6 桁入力で Amazon 側 JS が自動 submit。念のため数秒待って未遷移なら Enter フォールバック。
    await asyncio.sleep(3.0)
    if await detect_page(cdp) == EMAIL_OTP:
        log.info("OTP 自動 submit されず → Enter フォールバック")
        hid.press_key("\n")


async def handle_phone_input(cdp: CDPReader, hid: HumanInput, account, *, notify_sound: str) -> None:
    phone_cc = "+81"  # 国番号は日本固定
    log.info(f"page: 電話番号登録 {account.email} {phone_cc} {account.phone_num}")
    await human_delay(0.4, 0.8)
    if not account.phone_num:
        log.warning("電話番号データなし → 手動対応待ち")
        notify("Amazon 登録", f"{account.email}: 電話番号データなし。手動で入力してください", sound=notify_sound)
        return
    if not await set_select_value(cdp, SEL_PHONE_CC, phone_cc, label="phone_cc"):
        log.warning("国コード select が見つかりません")
    await human_delay(0.2, 0.4)
    if not await type_any(cdp, hid, SEL_PHONE_NUM, account.phone_num, label="phone_num"):
        log.error("電話番号フィールドが見つかりません")
        return
    await human_delay(0.4, 0.8)
    await click_any(cdp, hid, SEL_PHONE_SUBMIT, label="phone_submit")


async def _wait_manual_resolution(cdp: CDPReader, current_page: str, deadline: float) -> bool:
    """手動対応ページ（CAPTCHA/SMS_OTP）からの遷移をポーリングで待つ。

    遷移したら True、deadline 超過で False。
    """
    last_remind = time.time()
    while time.time() < deadline:
        await asyncio.sleep(4.0)
        page = await detect_page(cdp)
        if page != current_page:
            log.info(f"手動対応完了 → {page}")
            return True
        if time.time() - last_remind > 60:
            notify("Amazon 登録", "まだ手動対応待ちです", sound=None)
            last_remind = time.time()
    return False


# ===== オーケストレーション =====

async def run_registration(cdp: CDPReader, hid: HumanInput, guard: Guard, gmail: GmailClient, account, cfg) -> str:
    """1 アカウントの会員登録を完了 / エラー / タイムアウトまで実行しステータス文字列を返す。"""
    ts = now_jst_str()
    deadline = time.time() + cfg.retries.max_wait_sec_per_account
    complete_subs = cfg.urls.complete_substrings
    notify_sound = cfg.guard.notify_sound

    log.info(f"== 登録開始: {account.email} ==")
    await cdp.navigate(cfg.urls.register_start)
    await human_delay(2, 4)

    idle_unknown = 0
    last_handled = None  # (page, url) 直近に処理したシグネチャ（同一連打防止）

    while time.time() < deadline:
        # 汎用 WAF/403 ゲート（Amazon 固有の CAPTCHA はページ判定側で扱う）
        try:
            await guard.wait_clear()
        except GuardTimeout:
            return f"エラー: WAF/CAPTCHA タイムアウト（{now_jst_str()}）"

        url = await cdp.current_url()
        page = await detect_page(cdp)
        log.info(f"現在ページ: {page}  url={url[:80]}")

        if page == COMPLETE or any(s and s in url for s in complete_subs):
            log.info("登録完了を検知")
            return f"完了（{now_jst_str()}）"
        if page == ERROR:
            msg = await _error_message(cdp)
            return f"エラー: {msg}（{now_jst_str()}）"

        if page == UNKNOWN:
            idle_unknown += 1
            if idle_unknown > 8:
                return f"エラー: 不明なページが続く url={url[:100]}（{now_jst_str()}）"
            await asyncio.sleep(2.0)
            continue
        idle_unknown = 0

        # 手動対応ページ: 遷移するまで待つ
        if page == CAPTCHA:
            log.warning("CAPTCHA 検知 → 手動対応待ち")
            notify("Amazon CAPTCHA", f"{account.email}: 画像認証を手動で解いてください", sound=notify_sound)
            if not await _wait_manual_resolution(cdp, CAPTCHA, deadline):
                return f"エラー: CAPTCHA 手動対応タイムアウト（{now_jst_str()}）"
            continue
        if page == SMS_OTP:
            log.warning("SMS OTP 検知 → 手動入力待ち")
            notify("Amazon SMS確認コード", f"{account.email}: SMS コードを手動で入力してください", sound=notify_sound)
            if not await _wait_manual_resolution(cdp, SMS_OTP, deadline):
                return f"エラー: SMS OTP 手動対応タイムアウト（{now_jst_str()}）"
            continue

        # 自動入力ページ: 同一シグネチャの連打を避ける
        sig = (page, url)
        if sig == last_handled:
            # 同じページのままなら遷移を少し待ってから再評価
            await asyncio.sleep(2.0)
            if (await detect_page(cdp)) == page and (await cdp.current_url()) == url:
                log.info(f"{page}: 遷移しないため再試行")
        last_handled = sig

        if page == EMAIL_INPUT:
            await handle_email_input(cdp, hid, account)
        elif page == INTENT_CONFIRM:
            await handle_intent_confirm(cdp, hid)
        elif page == PASSWORD_INPUT:
            await handle_password_input(cdp, hid, account)
        elif page == EMAIL_OTP:
            await handle_email_otp(cdp, hid, gmail, account, notify_sound=notify_sound)
        elif page == PHONE_INPUT:
            await handle_phone_input(cdp, hid, account, notify_sound=notify_sound)

        # 遷移待ち（form submit 後のページ読み込み）
        await _wait_page_change(cdp, url, page, timeout=20.0)

    return f"エラー: タイムアウト（{ts}〜{now_jst_str()}）"


async def _wait_page_change(cdp: CDPReader, prev_url: str, prev_page: str, *, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        await asyncio.sleep(1.0)
        try:
            u = await cdp.current_url()
            p = await detect_page(cdp)
        except Exception:
            continue
        if u != prev_url or p != prev_page:
            return True
    return False
