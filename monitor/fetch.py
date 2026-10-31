"""기사 본문·언론사명 가져오기. 네이버 뉴스 페이지 → 언론사 원문 순으로 시도."""
from __future__ import annotations

import json
import logging
import time

import requests
import trafilatura
from bs4 import BeautifulSoup

from .press import press_from_domain
from .titles import full_title, is_truncated

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def _get(url: str, tries: int = 2) -> str | None:
    """일시 오류(타임아웃·5xx·429)는 한 번 더 시도한다."""
    for n in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8",
                                           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                             timeout=15, allow_redirects=True)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
                return r.text
            if r.status_code not in (429, 500, 502, 503, 504):
                log.info("본문 요청 실패 %s: HTTP %s", url[:80], r.status_code)
                return None
        except requests.RequestException as e:
            log.info("본문 요청 오류 %s: %s", url[:80], e)
        if n + 1 < tries:
            time.sleep(2)
    return None


def _jsonld_body(html: str) -> str | None:
    """JSON-LD의 articleBody(자바스크립트로 본문을 그리는 매체 대비)."""
    soup = BeautifulSoup(html, "lxml")
    best = ""
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                b = d.get("articleBody")
                if isinstance(b, str) and len(b) > len(best):
                    best = b
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
            elif isinstance(d, list):
                stack += d
    best = BeautifulSoup(best, "lxml").get_text("\n", strip=True) if "<" in best else best.strip()
    return best or None


def _article_tag_body(html: str) -> str | None:
    """trafilatura가 놓친 경우 <article>·본문 영역의 문단을 직접 모은다."""
    soup = BeautifulSoup(html, "lxml")
    for sel in ("article", "#article-view-content-div", "#articleBody", ".article_body", ".article-body",
                ".entry-content", ".post-content", "#article_body", ".news_body", "#newsEndContents"):
        tag = soup.select_one(sel)
        if tag:
            for t in tag.select("script, style, figure, figcaption, aside, nav"):
                t.decompose()
            ps = [p.get_text(" ", strip=True) for p in tag.select("p")]
            text = "\n".join(p for p in ps if len(p) > 20) or tag.get_text("\n", strip=True)
            if len(text) >= 200:
                return text
    return None


def _meta_title_date(html: str) -> tuple[str | None, str | None]:
    meta = trafilatura.extract_metadata(html)
    if not meta:
        return None, None
    return meta.title, meta.date


def _from_naver(html: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "lxml")
    press = None
    for sel, attr in (('meta[property="og:article:author"]', "content"),
                      ('meta[name="twitter:creator"]', "content"),
                      ("a.media_end_head_top_logo img", "alt"),
                      ("img.media_end_head_top_logo_img", "alt")):
        tag = soup.select_one(sel)
        if tag and tag.get(attr):
            press = tag[attr].split("|")[0].strip()
            break
    body_tag = soup.select_one("#dic_area") or soup.select_one("#newsct_article") \
        or soup.select_one("#articeBody") or soup.select_one("._article_content")
    body = None
    if body_tag:
        for t in body_tag.select("script, style, .img_desc, em.img_desc, .end_photo_org"):
            t.decompose()
        body = body_tag.get_text("\n", strip=True)
    return body, press


def _from_original(html: str) -> tuple[str | None, str | None]:
    body = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
    if not body or len(body) < 200:   # 정밀 모드가 놓치면 일반 모드 → JSON-LD → 본문 태그 순으로
        cands = [body or "",
                 trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True) or "",
                 _jsonld_body(html) or "", _article_tag_body(html) or ""]
        body = max(cands, key=len) or None
    meta = trafilatura.extract_metadata(html)
    site = meta.sitename if meta else None
    return body, site


def fetch_article(item: dict) -> dict:
    """item에 body, press 채워서 반환. 실패하면 요약문(description)으로 대체."""
    body, press = None, None
    if item.get("naver_url"):
        html = _get(item["naver_url"])
        if html:
            body, press = _from_naver(html)
            if not item.get("title"):
                item["title"], item["_meta_date"] = _meta_title_date(html)
            elif is_truncated(item["title"]):   # 네이버 API가 '...'로 자른 제목 복원
                item["title"] = full_title(item["title"], html) or item["title"]
    if not body or len(body) < 200 or not item.get("title") or is_truncated(item.get("title", "")):
        html = _get(item["url"])
        if html:
            if not item.get("title"):
                item["title"], item["_meta_date"] = _meta_title_date(html)
            elif is_truncated(item["title"]):
                item["title"] = full_title(item["title"], html) or item["title"]
            b2, site = _from_original(html)
            if b2 and len(b2) > len(body or ""):
                body = b2
            press = press or press_from_domain(item["url"]) or site
    # 원문 페이지가 막혀도 RSS 피드에 본문이 있으면 그것을 쓴다(해외 매체)
    rb = (item.get("rss_body") or "").strip()
    if (not body or len(body) < 200) and len(rb) > len(body or ""):
        body = rb
    if not body or len(body) < 200:
        log.info("본문 미확보(%d자): %s", len(body or ""), item.get("url", "")[:90])
    press = press or press_from_domain(item["url"])
    item["body"] = (body or "").strip()
    item["body_ok"] = bool(body and len(body) >= 200)
    item["press"] = press or item.get("press") or ""
    return item
