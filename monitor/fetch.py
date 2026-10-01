"""기사 본문·언론사명 가져오기. 네이버 뉴스 페이지 → 언론사 원문 순으로 시도."""
from __future__ import annotations

import logging

import requests
import trafilatura
from bs4 import BeautifulSoup

from .press import press_from_domain

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def _get(url: str) -> str | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"},
                         timeout=15, allow_redirects=True)
        if r.status_code != 200:
            return None
        r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
        return r.text
    except requests.RequestException:
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
    if not body or len(body) < 200 or not item.get("title"):
        html = _get(item["url"])
        if html:
            if not item.get("title"):
                item["title"], item["_meta_date"] = _meta_title_date(html)
            b2, site = _from_original(html)
            if b2 and len(b2) > len(body or ""):
                body = b2
            press = press or press_from_domain(item["url"]) or site
    press = press or press_from_domain(item["url"])
    item["body"] = (body or "").strip()
    item["body_ok"] = bool(body and len(body) >= 200)
    item["press"] = press or item.get("press") or ""
    return item
