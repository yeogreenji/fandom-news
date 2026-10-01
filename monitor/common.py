"""공통 유틸: 경로, 설정 로드, 시간, JSON 입출력."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DOCS_DIR = ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"
DAYS_DIR = DATA_DIR / "days"
STATE_FILE = ROOT / "state" / "state.json"

KST = timezone(timedelta(hours=9))

SECTIONS = {
    "own": "bemyfriends & Dreamus Company",
    "fandom_music": "Fandom & Music IP business",
    "ent_content": "Entertainment & Contents IP business",
    "ai_tech": "AI & Tech business",
}
SECTION_ORDER = list(SECTIONS.keys())


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_text(name: str) -> str:
    return (CONFIG_DIR / name).read_text(encoding="utf-8")


def settings() -> dict:
    return load_yaml("settings.yaml")


def now_kst() -> datetime:
    return datetime.now(KST)


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def url_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def date_label(dt: datetime) -> str:
    """위클리 표기: 26.09.24"""
    return dt.astimezone(KST).strftime("%y.%m.%d")


def update_index() -> None:
    dates = sorted((p.stem for p in DAYS_DIR.glob("*.json")), reverse=True)
    write_json(DATA_DIR / "index.json", {
        "dates": dates,
        "updated": now_kst().isoformat(timespec="minutes"),
        "sections": SECTIONS,
    })
