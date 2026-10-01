"""기사 후보 수집: 네이버 뉴스 검색 API + 해외 매체 RSS."""
from __future__ import annotations

import html
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

from .common import KST

log = logging.getLogger(__name__)

# 2026-07-31부로 네이버 개발자센터 검색 API 신규 발급 종료 → NAVER API HUB(네이버 클라우드)로 이관
# 기존 개발자센터 키(2027-06-30까지 사용 가능)가 있으면 NAVER_API_MODE=legacy
HUB_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
LEGACY_URL = "https://openapi.naver.com/v1/search/news.json"
TAG_RE = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def _naver_endpoint() -> tuple[str, dict]:
    cid = os.environ.get("NAVER_CLIENT_ID")
    secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 없습니다.")
    if os.environ.get("NAVER_API_MODE", "hub").lower() == "legacy":
        return LEGACY_URL, {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    return HUB_URL, {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}


def naver_search(query: str, since: datetime, limit: int = 200) -> list[dict]:
    """최신순으로 받아오다 since 이전 기사가 나오면 멈춘다."""
    url, headers = _naver_endpoint()
    out: list[dict] = []
    start = 1
    while start <= min(limit, 1000):
        display = min(100, limit - start + 1, 1000 - start + 1)
        params = {"query": query, "display": display, "start": start, "sort": "date"}
        for attempt in range(3):
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 429:  # 초당 호출 제한
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            log.warning("네이버 API 오류 %s (%s): %s", r.status_code, query, r.text[:200])
            break
        items = r.json().get("items", [])
        if not items:
            break
        reached_old = False
        for it in items:
            try:
                pub = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            except Exception:
                continue
            if pub < since:
                reached_old = True
                continue
            out.append({
                "title": clean(it.get("title")),
                "description": clean(it.get("description")),
                "url": it.get("originallink") or it.get("link"),
                "naver_url": it.get("link") if "naver.com" in (it.get("link") or "") else None,
                "pub_date": pub.isoformat(),
                "lang": "ko",
            })
        if reached_old or len(items) < display:
            break
        start += display
        time.sleep(0.12)
    return out


def rss_fetch(url: str, since: datetime) -> list[dict]:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (news-monitor)"})
        feed = feedparser.parse(r.content)
    except Exception as e:  # 해외 피드 하나 실패해도 전체는 계속
        log.warning("RSS 실패 %s: %s", url, e)
        return []
    out = []
    for e in feed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if not t:
            continue
        pub = datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST)  # feedparser는 UTC 기준
        if pub < since:
            continue
        out.append({
            "title": clean(e.get("title")),
            "description": clean(e.get("summary"))[:400],
            "url": e.get("link"),
            "naver_url": None,
            "pub_date": pub.isoformat(),
            "lang": "en",
        })
    return out
