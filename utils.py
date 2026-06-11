from __future__ import annotations
import asyncio
import logging
import random
import re
from datetime import datetime


def setup_logger(name: str = "pco", level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    h.setFormatter(fmt)
    logger.addHandler(h)
    return logger


def now_jst_str() -> str:
    d = datetime.now()
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d} {d.hour:02d}:{d.minute:02d}"


async def human_delay(min_sec: float = 0.5, max_sec: float = 1.5) -> None:
    base = random.uniform(min_sec, max_sec)
    jitter = random.uniform(-0.1, 0.1)
    total = max(0.1, base + jitter)
    await asyncio.sleep(total)


def normalize_gmail(email: str) -> str:
    if not email:
        return ""
    s = email.strip().lower()
    parts = s.split("@")
    if len(parts) != 2:
        return s
    local, domain = parts
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
        return f"{local}@gmail.com"
    return f"{local}@{domain}"


CODE_RE_6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")
CODE_RE_ANY = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def extract_otp_code(text: str) -> str | None:
    m = CODE_RE_6.search(text or "")
    if m:
        return m.group(1)
    m = CODE_RE_ANY.search(text or "")
    return m.group(1) if m else None
