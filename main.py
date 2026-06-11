"""Amazon 会員登録自動化ツール（CLI）。

PCO 抽選ツールのオーケストレーションを流用し、1 アカウントの処理を
amazon_flow.run_registration（8 ページ状態機械）に差し替えたもの。

モード:
  bulk    全アカウントを処理
  resume  未完了（status に「完了」を含まない）アカウントのみ処理
"""

from __future__ import annotations
import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

import chrome_launcher
from amazon_flow import run_registration
from cdp_reader import CDPReader
from config_loader import load_config
from gas_client import Account, GasClient
from gmail_client import GmailClient
from guard import Guard
from human_input import HumanInput
from notifier import DiscordNotifier
from utils import human_delay, now_jst_str, setup_logger
from wake_lock import WakeLock

log = setup_logger("amazon.main")

# WebAuthn(パスキー) を無効化する初期化スクリプト。
# Amazon の JS が走る前に評価し、PublicKeyCredential を未定義・credentials.create/get を
# 拒否させることで、macOS の Touch ID パスキー保存ダイアログを出させず、
# Amazon にも最初からパスキー画面をスキップさせる（パスワード経路へ）。
WEBAUTHN_DISABLE_JS = """
(() => {
  try { Object.defineProperty(window, 'PublicKeyCredential', { value: undefined, configurable: true }); } catch (e) {}
  try {
    if (navigator.credentials) {
      const reject = () => Promise.reject(new DOMException('disabled', 'NotAllowedError'));
      navigator.credentials.create = reject;
      navigator.credentials.get = reject;
    }
  } catch (e) {}
})();
"""

# NOTE: human_input は画面を占有する HID イベントを発行するため、
# このスクリプトは絶対に並列実行しないこと（複数プロセス不可）。


class BrowserSession:
    """Chrome プロセス + CDP 接続 + HumanInput + Guard を束ねたライフサイクル。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.handle = None
        self.cdp: CDPReader | None = None
        self.hid: HumanInput | None = None
        self.guard: Guard | None = None

    async def open(self, chrome_cfg=None) -> None:
        cc = chrome_cfg or self.cfg.chrome
        self.handle = await chrome_launcher.launch(cc)
        self.cdp = await CDPReader.from_browser(self.handle.browser)
        # パスキー無効化スクリプトを全ナビゲーション前に登録（Touch ID ダイアログ抑止）
        try:
            await self.cdp.add_init_script(WEBAUTHN_DISABLE_JS)
        except Exception as e:
            log.warning(f"WebAuthn 無効化スクリプト登録に失敗（続行）: {e}")
        await self.cdp.get_window_position()
        self.hid = HumanInput(self.cfg.input, dryrun=self.cfg.dryrun)
        self.guard = Guard(self.cdp, self.cfg.guard)

    async def close(self) -> None:
        if self.cdp:
            try:
                await self.cdp.close()
            except Exception:
                pass
            self.cdp = None
        if self.handle:
            chrome_launcher.close(self.handle)
            self.handle = None

    async def reopen_fresh(self) -> None:
        await self.close()
        await asyncio.sleep(2.0)
        if getattr(self.cfg.chrome, "profile_rotation_enabled", False):
            log.info("→ profile rotation 有効: 別プロファイルで reopen")
            await self.open()
            return
        parent = os.path.dirname(self.cfg.chrome.user_data_dir.rstrip("/\\"))
        fresh_dir = os.path.join(parent, f"amazon-fresh-{int(time.time())}")
        log.info(f"→ 新規プロファイルで reopen: {fresh_dir}")
        fresh_cfg = replace(self.cfg.chrome, user_data_dir=fresh_dir)
        await self.open(chrome_cfg=fresh_cfg)


async def process_account(
    cfg, session: BrowserSession, gas: GasClient, gmail: GmailClient, account: Account,
) -> str:
    cdp, hid, guard = session.cdp, session.hid, session.guard
    ts = now_jst_str()
    log.info(f"== 処理開始: {account.email} ==")

    # 処理中マーク（観測用、claim 排他は GAS 側に無いため簡易）
    try:
        gas.record_result(account.email, f"実行中（{ts}）")
    except Exception as e:
        log.warning(f"実行中マーク失敗（続行）: {e}")

    try:
        status = await run_registration(cdp, hid, guard, gmail, account, cfg)
    except Exception as e:
        log.exception("run_registration 例外")
        status = f"エラー: {e}（{now_jst_str()}）"
    return status


def _status_group_key(status: str) -> str:
    s = status or ""
    for sep in ("（", "("):
        idx = s.find(sep)
        if idx > 0:
            return s[:idx].strip()
    return s.strip() or "(empty)"


ProgressCallback = Callable[[dict], None]


def _build_status_dict(state: str, done: int, total: int, results: list[tuple[str, str]]) -> dict:
    failed = sum(1 for _, s in results if s.startswith("エラー"))
    breakdown = Counter(_status_group_key(s) for _, s in results)
    return {
        "state": state,
        "done": done,
        "total": total,
        "success": len(results) - failed,
        "failed": failed,
        "breakdown": dict(breakdown),
    }


def _emit_progress(
    *, state: str, done: int, total: int, results: list[tuple[str, str]],
    status_file: Optional[Path], progress_callback: Optional[ProgressCallback],
) -> None:
    data = _build_status_dict(state, done, total, results)
    if status_file is not None:
        try:
            status_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning(f"status file 書き出し失敗: {e}")
    if progress_callback is not None:
        try:
            progress_callback(data)
        except Exception as e:
            log.warning(f"progress_callback 失敗: {e}")


async def run_one_pass(
    cfg, mode: str, notifier: DiscordNotifier,
    *, status_file: Optional[Path] = None,
    results_accum: Optional[list[tuple[str, str]]] = None,
    global_total_hint: Optional[int] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> list[tuple[str, str]]:
    gas = GasClient(cfg)
    gmail = GmailClient(cfg.gmail)
    gmail._ensure_service()

    if mode == "bulk":
        accounts = gas.fetch_accounts()
    elif mode == "resume":
        accounts = gas.get_incomplete_accounts()
    else:
        raise ValueError(f"unknown mode: {mode}")

    if cfg.skip_statuses:
        before = len(accounts)

        def _should_skip(acc) -> bool:
            s = (acc.status or "")
            return any(kw and kw in s for kw in cfg.skip_statuses)

        skipped = [a for a in accounts if _should_skip(a)]
        accounts = [a for a in accounts if not _should_skip(a)]
        if skipped:
            log.info(f"ステータスでスキップ: {len(skipped)}/{before} 件")

    if cfg.processing_order == "bottom":
        accounts = list(reversed(accounts))

    log.info(f"対象アカウント: {len(accounts)} 件 (mode={mode})")
    if not accounts:
        return []

    session = BrowserSession(cfg)
    try:
        await session.open()
    except Exception as e:
        notifier.critical("ブラウザ起動失敗", f"{type(e).__name__}: {e}")
        raise

    results: list[tuple[str, str]] = []
    try:
        try:
            for i, acc in enumerate(accounts, start=1):
                log.info(f"--- [{i}/{len(accounts)}] {acc.email} ---")
                if not acc.password:
                    log.info("パスワード未設定 → スキップ")
                    continue

                status = await process_account(cfg, session, gas, gmail, acc)
                results.append((acc.email, status))

                try:
                    gas.record_result(acc.email, status)
                    log.info(f"記録完了: {status}")
                except Exception as e:
                    log.warning(f"record_result 失敗: {e}")

                # エラー後は fingerprint をリフレッシュ
                if status.startswith("エラー"):
                    log.warning(f"エラー検知 → ブラウザリセット: {status}")
                    try:
                        await session.reopen_fresh()
                    except Exception as e:
                        log.error(f"ブラウザリセット失敗: {e}")
                        notifier.critical("ブラウザリセット失敗", f"{type(e).__name__}: {e}")

                every = cfg.notifications.progress_every
                if every > 0 and len(results) % every == 0:
                    bd = Counter(_status_group_key(s) for _, s in results)
                    notifier.progress(len(results), len(accounts), dict(bd))

                if results_accum is not None:
                    results_accum.append((acc.email, status))
                    _emit_progress(
                        state="running", done=len(results_accum),
                        total=global_total_hint or len(accounts),
                        results=results_accum, status_file=status_file,
                        progress_callback=progress_callback,
                    )
                else:
                    _emit_progress(
                        state="running", done=len(results), total=len(accounts),
                        results=results, status_file=status_file,
                        progress_callback=progress_callback,
                    )

                await human_delay(4, 8)
        except Exception as e:
            notifier.critical(
                "スクリプト異常終了",
                f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}",
            )
            raise
    finally:
        await session.close()
    return results


async def run(
    cfg, initial_mode: str, *,
    status_file: Optional[Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> None:
    notifier = DiscordNotifier(cfg.notifications)
    all_results: list[tuple[str, str]] = []
    try:
        await run_one_pass(
            cfg, initial_mode, notifier,
            status_file=status_file, results_accum=all_results,
            progress_callback=progress_callback,
        )

        if cfg.auto_resume.enabled and cfg.auto_resume.max_rounds > 0:
            for round_idx in range(1, cfg.auto_resume.max_rounds + 1):
                wait = cfg.auto_resume.interval_sec
                log.info(f"auto_resume: {wait}s 待機してから round {round_idx}/{cfg.auto_resume.max_rounds}")
                await asyncio.sleep(wait)
                round_results = await run_one_pass(
                    cfg, "resume", notifier,
                    status_file=status_file, results_accum=all_results,
                    progress_callback=progress_callback,
                )
                if not round_results:
                    log.info("未処理アカウントなし → auto_resume 終了")
                    break
                if cfg.auto_resume.give_up_if_no_progress:
                    failed_this = sum(1 for _, s in round_results if s.startswith("エラー"))
                    if len(round_results) - failed_this == 0:
                        log.info("成功 0 件 → auto_resume 打ち切り")
                        break
    finally:
        total = len(all_results)
        failed = sum(1 for _, s in all_results if s.startswith("エラー"))
        success = total - failed
        breakdown = Counter(_status_group_key(s) for _, s in all_results)
        notifier.summary(total, success, failed, dict(breakdown))
        _emit_progress(
            state="done", done=total, total=total, results=all_results,
            status_file=status_file, progress_callback=progress_callback,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=["bulk", "resume"],
        help="bulk=全件 / resume=未完了のみ",
    )
    parser.add_argument("--status-file", default=None, help="進捗 JSON をこのパスに書き出し")
    args = parser.parse_args()

    status_file = Path(args.status_file) if args.status_file else None
    cfg = load_config()

    with WakeLock(enabled=cfg.wake_lock.enabled):
        try:
            asyncio.run(run(cfg, args.mode, status_file=status_file))
        except KeyboardInterrupt:
            log.info("Interrupted")
            sys.exit(130)


if __name__ == "__main__":
    main()
