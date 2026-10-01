"""영업일·수집 구간 계산.

- 영업일: 월~금 중 한국 공휴일(대체공휴일·선거일 포함)과 settings의 extra_holidays가 아닌 날
- 수집 구간: 직전 영업일 기준시각(09:30) ~ 오늘 기준시각
  → 월요일은 금 09:30~월 09:30, 연휴 다음 날은 연휴 전체를 묶음
- 게재 상한: 묶인 일수에 따라 올라감 (1일 30 / 2일 40 / 3일 50 / … 최대 70)
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from functools import lru_cache

import holidays

from .common import KST


@lru_cache(maxsize=None)
def _kr(year: int):
    return holidays.KR(years=[year])


def is_workday(d: date, cfg: dict) -> bool:
    ds = d.isoformat()
    if ds in {str(x) for x in cfg.get("extra_workdays", []) or []}:
        return True
    if ds in {str(x) for x in cfg.get("extra_holidays", []) or []}:
        return False
    return d.weekday() < 5 and d not in _kr(d.year)


def holiday_name(d: date) -> str | None:
    return _kr(d.year).get(d)


def cutoff(d: date, cfg: dict) -> datetime:
    hh, mm = map(int, str(cfg.get("cutoff_time", "09:30")).split(":"))
    return datetime.combine(d, time(hh, mm), tzinfo=KST)


def prev_workday(d: date, cfg: dict) -> date:
    x = d - timedelta(days=1)
    while not is_workday(x, cfg):
        x -= timedelta(days=1)
    return x


def window(now: datetime, cfg: dict, last_end: str | None) -> tuple[datetime, datetime]:
    """(시작, 끝). 끝은 오늘 기준시각(실행이 늦어져도 고정). 시작은 직전 수집 끝(실패한 날이 있어도 빈틈 없이)."""
    today = now.astimezone(KST).date()
    end = cutoff(today, cfg)
    if now < end:  # 기준시각 전 수동 실행이면 지금까지
        end = now
    start = cutoff(prev_workday(today, cfg), cfg)
    if last_end:
        le = datetime.fromisoformat(last_end)
        if end - timedelta(days=14) < le < start:  # 직전 실행이 실패해 비어 있는 구간까지 포함
            start = le
    return start, end


def days_covered(start: datetime, end: datetime) -> int:
    return max(1, math.ceil((end - start).total_seconds() / 86400 - 0.01))


def caps(start: datetime, end: datetime, cfg: dict) -> tuple[int, int]:
    """(게재 상한, 본문 검토 상한)"""
    n = days_covered(start, end)
    c = cfg.get("cap_base", 30) + cfg.get("cap_per_extra_day", 10) * (n - 1)
    c = min(c, cfg.get("cap_max", 70))
    return c, math.ceil(c * cfg.get("review_ratio", 1.4))
