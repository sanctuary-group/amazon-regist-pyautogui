from __future__ import annotations
import asyncio
import base64
import os
import time
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config_loader import GmailConfig
from utils import extract_otp_code, setup_logger

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
log = setup_logger("pco.gmail")


class GmailClient:
    def __init__(self, cfg: GmailConfig):
        self.cfg = cfg
        self._service = None

    def _get_credentials(self) -> Credentials:
        creds: Optional[Credentials] = None
        if os.path.exists(self.cfg.token_file):
            creds = Credentials.from_authorized_user_file(self.cfg.token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.cfg.credentials_file):
                    raise FileNotFoundError(
                        f"OAuth クライアント '{self.cfg.credentials_file}' が見つかりません。"
                        "Google Cloud Console で作成してプロジェクト直下に配置してください。"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(self.cfg.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.cfg.token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds

    def _ensure_service(self):
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self._get_credentials(), cache_discovery=False)
        return self._service

    def _list_messages(
        self, query: str, max_results: int = 20, *, include_spam_trash: bool = True,
    ) -> list[dict]:
        svc = self._ensure_service()
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=max_results,
            includeSpamTrash=include_spam_trash,
        ).execute()
        return resp.get("messages", []) or []

    def _get_message(self, msg_id: str) -> dict:
        svc = self._ensure_service()
        return svc.users().messages().get(userId="me", id=msg_id, format="full").execute()

    @staticmethod
    def _decode_body(payload: dict) -> str:
        if not payload:
            return ""
        if payload.get("parts"):
            for p in payload["parts"]:
                t = GmailClient._decode_body(p)
                if t:
                    return t
        data = (payload.get("body") or {}).get("data")
        if not data:
            return ""
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def find_latest_code(self, query: str, since_ms: Optional[int] = None) -> Optional[str]:
        msgs = self._list_messages(query)
        log.info(f"Gmail: {len(msgs)}件ヒット, sinceMs={since_ms}")
        for m in msgs:
            full = self._get_message(m["id"])
            internal_ms = int(full.get("internalDate", "0") or 0)
            if since_ms and internal_ms and internal_ms < (since_ms - 2000):
                continue
            body = self._decode_body(full.get("payload") or {}) or full.get("snippet", "")
            code = extract_otp_code(body)
            if code:
                log.info(f"OTP検出: {code}")
                return code
        return None

    def find_apply_completion(
        self, account_email: str, *, days: int = 7,
        target_strings: Optional[list[str]] = None,
    ) -> dict:
        """応募完了メールの有無を判定。

        Returns:
            {"found": bool, "matched_products": list[str], "message_count": int}
            target_strings 指定時は本文内の商品名 hit のみカウント。
            target_strings 未指定 (空) 時は message_count > 0 なら found=True。
        """
        from datetime import datetime, timedelta
        after = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        query = (
            f'after:{after} to:{account_email} '
            f'subject:"応募完了のお知らせ" "ポケモンセンターオンライン"'
        )
        try:
            msgs = self._list_messages(query, max_results=50)
        except Exception as e:
            log.warning(f"Gmail 検索エラー ({account_email}): {e}")
            return {"found": False, "matched_products": [], "message_count": 0}

        if not target_strings:
            return {
                "found": len(msgs) > 0,
                "matched_products": [],
                "message_count": len(msgs),
            }
        matched: set[str] = set()
        for m in msgs:
            try:
                full = self._get_message(m["id"])
                body = self._decode_body(full.get("payload") or {}) or full.get("snippet", "")
            except Exception as e:
                log.warning(f"Gmail 取得エラー ({account_email} msg={m.get('id')}): {e}")
                continue
            for t in target_strings:
                if t and t in body:
                    matched.add(t)
        return {
            "found": len(matched) > 0,
            "matched_products": sorted(matched),
            "message_count": len(msgs),
        }

    async def wait_otp(self, login_email: Optional[str] = None, since_ms: Optional[int] = None) -> Optional[str]:
        """OTP メールが届くまでポーリング。login_email が与えられれば検索クエリに含める（転送メール対応）"""
        query = self.cfg.query
        if login_email:
            query = f'{query} (to:{login_email} OR "{login_email}")'
        end = time.time() + self.cfg.timeout_sec
        fallback_at = time.time() + min(15, self.cfg.timeout_sec / 3)
        fallback_tried = False
        while time.time() < end:
            try:
                code = self.find_latest_code(query, since_ms)
                if code:
                    return code
            except Exception as e:
                log.warning(f"Gmail検索エラー: {e}")
            if not fallback_tried and time.time() >= fallback_at:
                try:
                    code = self.find_latest_code(query, None)
                    if code:
                        return code
                except Exception:
                    pass
                finally:
                    fallback_tried = True
            await asyncio.sleep(5)
        log.warning("OTP wait timeout")
        return None
