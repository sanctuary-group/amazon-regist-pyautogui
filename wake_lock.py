from __future__ import annotations
import platform
import subprocess
from typing import Optional

from utils import setup_logger

log = setup_logger("pco.wake")


_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


class WakeLock:
    """スクリプト実行中 OS のスリープ/ディスプレイオフを抑制する。

    macOS:   `caffeinate -dis` サブプロセスで抑制 (プロセス終了で自動解除)
    Windows: SetThreadExecutionState で ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    Linux:   no-op (systemd-inhibit を試行、失敗したら警告)
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._proc: Optional[subprocess.Popen] = None
        self._windows_prev_state: Optional[int] = None
        self._system = platform.system()

    def __enter__(self) -> "WakeLock":
        if not self.enabled:
            log.info("wake lock: disabled by config")
            return self
        try:
            if self._system == "Darwin":
                self._proc = subprocess.Popen(
                    ["caffeinate", "-dis"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                log.info(f"wake lock: caffeinate pid={self._proc.pid}")
            elif self._system == "Windows":
                import ctypes
                flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
                prev = ctypes.windll.kernel32.SetThreadExecutionState(flags)
                self._windows_prev_state = prev
                log.info(f"wake lock: SetThreadExecutionState flags=0x{flags:x}")
            else:
                try:
                    self._proc = subprocess.Popen(
                        ["systemd-inhibit", "--what=idle:sleep",
                         "--who=pco-lottery", "--why=automation",
                         "sleep", "infinity"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    log.info(f"wake lock: systemd-inhibit pid={self._proc.pid}")
                except FileNotFoundError:
                    log.warning("wake lock: systemd-inhibit not available, skipping")
        except Exception as e:
            log.warning(f"wake lock: failed to engage ({e}), continuing without")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception as e:
                log.warning(f"wake lock: failed to stop subprocess ({e})")
            self._proc = None
        if self._system == "Windows" and self._windows_prev_state is not None:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            except Exception as e:
                log.warning(f"wake lock: failed to restore Windows state ({e})")
            self._windows_prev_state = None
        log.info("wake lock: released")
