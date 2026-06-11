from __future__ import annotations
import requests
from dataclasses import dataclass
from config_loader import Config


@dataclass
class Account:
    email: str
    password: str = ""
    name: str = ""
    phone_num: str = ""
    status: str = ""

    @classmethod
    def from_row(cls, row) -> "Account":
        """GAS fetchAccounts は配列行 [email, password, name, phone_num] を返す。

        国番号は日本固定（+81）のためシート列には持たない。
        """
        if isinstance(row, dict):
            return cls(
                email=str(row.get("email") or "").strip(),
                password=str(row.get("password") or "").strip(),
                name=str(row.get("name") or "").strip(),
                phone_num=str(row.get("phone_num") or "").strip(),
                status=str(row.get("status") or "").strip(),
            )
        row = list(row) if row is not None else []

        def _g(i: int) -> str:
            return str(row[i]).strip() if len(row) > i and row[i] is not None else ""

        return cls(
            email=_g(0), password=_g(1), name=_g(2),
            phone_num=_g(3), status=_g(4),
        )


class GasClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _post(self, payload: dict, *, timeout: int = 90, retries: int = 3) -> dict:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                res = requests.post(
                    self.cfg.gas_webapp_url,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                    timeout=timeout,
                )
                res.raise_for_status()
                return res.json()
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt < retries:
                    import time as _t
                    _t.sleep(2 * attempt)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def fetch_accounts(self) -> list[Account]:
        data = self._post({
            "action": "fetchAccounts",
            "sheetName": self.cfg.sheet_name,
            "startColLetter": self.cfg.start_col_letter,
            # データ列 + status 列(E) も取得（skip_statuses 判定のため）
            "numCols": str(self.cfg.num_cols + 1),
        })
        if not data.get("ok"):
            raise RuntimeError(f"fetchAccounts failed: {data}")
        rows = data.get("accounts", []) or []
        accounts = [Account.from_row(row) for row in rows]
        return [a for a in accounts if a.email]

    def get_incomplete_accounts(self) -> list[Account]:
        data = self._post({
            "action": "getIncompleteAccounts",
            "sheetName": self.cfg.sheet_name,
            "startColLetter": self.cfg.start_col_letter,
            "numCols": str(self.cfg.num_cols),
        })
        if not data.get("ok"):
            raise RuntimeError(f"getIncompleteAccounts failed: {data}")
        rows = data.get("accounts", []) or []
        accounts = [Account.from_row(row) for row in rows]
        return [a for a in accounts if a.email]

    def record_result(self, email: str, status: str) -> dict:
        # GAS 側で status 列 = startCol + numCols に算出。日時は GAS が status に内包して書込む。
        return self._post({
            "action": "recordResult",
            "sheetName": self.cfg.sheet_name,
            "startColLetter": self.cfg.start_col_letter,
            "emailColLetter": self.cfg.email_col_letter,
            "numCols": str(self.cfg.num_cols),
            "email": email,
            "status": status,
        })
