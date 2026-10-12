"""잘린 기사 제목('...'로 끝나는 제목) 복원.

네이버 검색 API는 긴 제목을 '...'로 잘라서 준다. 네이버 기사 페이지나 언론사 원문 페이지의
제목 태그에서 전체 제목을 찾아 바꾼다. 찾은 제목이 잘린 제목과 앞부분이 같을 때만 바꾼다.

사이트에 이미 올라간 기사 복원:  python -m monitor.titles
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_TRUNC = re.compile(r"\s*(\.\.\.|…)\s*$")
# 원문 제목 태그 뒤에 붙는 사이트명 구분자
_SEPS = (" | ", " - ", " – ", " — ", " :: ", " : 네이버", " < ", " │ ", "ㅣ")


def is_truncated(title: str) -> bool:
    return bool(_TRUNC.search(title or ""))


def _norm(s: str) -> str:
    s = (s or "").replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", "", s)


def _candidates(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for sel in ("#title_area", "h2.media_end_head_headline", "h2#title_area span"):
        t = soup.select_one(sel)
        if t and t.get_text(strip=True):
            out.append(t.get_text(" ", strip=True))
    for sel in ('meta[property="og:title"]', 'meta[name="twitter:title"]', 'meta[name="title"]'):
        t = soup.select_one(sel)
        if t and t.get("content"):
            out.append(t["content"].strip())
    if soup.title and soup.title.string:
        out.append(soup.title.string.strip())
    for t in soup.select("h1, h2, h3")[:8]:
        if t.get_text(strip=True):
            out.append(t.get_text(" ", strip=True))
    return out


def full_title(truncated: str, html: str | None) -> str | None:
    """html 안에서 truncated의 전체 제목을 찾는다. 못 찾으면 None."""
    if not html or not is_truncated(truncated):
        return None
    prefix = _norm(_TRUNC.sub("", truncated))
    if len(prefix) < 8:
        return None
    for cand in _candidates(html):
        cand = re.sub(r"\s+", " ", cand).strip()
        n = _norm(cand)
        if not n.startswith(prefix) or len(n) <= len(prefix):
            continue
        # 잘린 지점 뒤에 붙은 사이트명 떼기
        keep_chars = len(_TRUNC.sub("", truncated).rstrip())
        tail_cut = len(cand)
        for sep in _SEPS:
            i = cand.find(sep, keep_chars)
            if i != -1:
                tail_cut = min(tail_cut, i)
        title = cand[:tail_cut].strip()
        if _norm(title).startswith(prefix) and len(_norm(title)) > len(prefix) and not is_truncated(title):
            return title
    return None


def repair(item: dict, get) -> bool:
    """item['title']이 잘려 있으면 네이버 → 원문 순으로 전체 제목을 찾아 바꾼다. get은 url→html 함수."""
    if not is_truncated(item.get("title", "")):
        return False
    for url in (item.get("naver_url"), item.get("url")):
        if not url:
            continue
        t = full_title(item["title"], get(url))
        if t:
            item["title"] = t
            return True
    return False


def main() -> None:
    from .common import DAYS_DIR, read_json, write_json
    from .fetch import _get
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fixed = left = 0
    for p in sorted(DAYS_DIR.glob("*.json")):
        data = read_json(p, {})
        changed = False
        for it in data.get("items", []):
            if not is_truncated(it.get("title", "")):
                continue
            old = it["title"]
            if repair(it, _get):
                fixed += 1
                changed = True
                log.info("제목 복원: %s → %s", old, it["title"])
            else:
                left += 1
                log.warning("제목 복원 실패: %s (%s)", old, it.get("url"))
        if changed:
            write_json(p, data)
    log.info("제목 복원 %d건, 실패 %d건", fixed, left)


if __name__ == "__main__":
    main()
