"""ファイルパスの統一解決。

開発時 (`python gui.py`) と PyInstaller バンドル時 (`PCO応募.app`) の両方で動作する。

- `resource_path(name)`: 読み取り専用の同梱リソース (テンプレート、main.py 等) を解決
- `app_data_dir()`: ユーザー固有の設定/トークンを置く書き込み可能ディレクトリ
"""
from __future__ import annotations
import os
import platform
import sys
from pathlib import Path


APP_NAME = "Amazon登録"


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(name: str) -> Path:
    """同梱リソースのパス。バンドル時は sys._MEIPASS、開発時はスクリプトの隣。"""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    return base / name


def app_data_dir() -> Path:
    """ユーザー固有データ (config.yaml, credentials.json, token.json) のディレクトリ。

    バンドル時:
      macOS:   ~/Library/Application Support/PCO応募
      Windows: %APPDATA%/PCO応募
      Linux:   ~/.config/PCO応募
    開発時: スクリプトディレクトリ (既存の挙動を維持)
    """
    if not is_frozen():
        return Path(__file__).resolve().parent

    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    """config.yaml の実際のパス。app_data_dir 配下。"""
    return app_data_dir() / "config.yaml"


def credentials_path() -> Path:
    return app_data_dir() / "credentials.json"


def token_path() -> Path:
    return app_data_dir() / "token.json"


def status_file_path() -> Path:
    return app_data_dir() / ".amazon-status.json"


def ensure_first_run_files() -> dict[str, bool]:
    """初回起動時、テンプレートを app_data_dir にコピー。

    Returns: {"config_copied": bool, "credentials_missing": bool}
    """
    result = {"config_copied": False, "credentials_missing": False}
    cfg = config_path()
    if not cfg.exists():
        template = resource_path("config.yaml.template")
        if template.exists():
            cfg.write_bytes(template.read_bytes())
            result["config_copied"] = True
    if not credentials_path().exists():
        result["credentials_missing"] = True
    return result
