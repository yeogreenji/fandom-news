"""담당자 수동 조정: 기사 직접 추가 / 게재 기사 빼기.
GitHub Actions의 'Manual edit' 워크플로에서 호출된다."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

from . import llm
from .common import DAYS_DIR, KST, now_kst, read_json, settings, update_index, url_id, write_json
from .fetch import fetch_article
from .run_daily import sort_records, to_record
from .textutil import domain

log = logging.getLogger("manual")


def add(url: str, section: str | None, day: str | None) -> None:
    cfg = settings()
    day = day or now_kst().strftime("%Y-%m-%d")
    item = {"url": url, "naver_url": url if "naver.com" in domain(url) else None, "title": "",
            "description": "", "lang": "ko", "queries": ["수동 추가"],
            "section_hints": [section or "fandom_music"], "cluster_size": 1}
    fetch_article(item)
    if not item.get("title"):
        raise SystemExit("기사 제목을 가져오지 못했습니다. 링크를 확인해주세요.")
    try:
        pub = datetime.fromisoformat(item.get("_meta_date") or "").replace(tzinfo=KST)
    except ValueError:
        pub = now_kst()
    item["pub_date"] = pub.isoformat()
    item["id"] = url_id(url)
    if section:
        item["triage"] = {"section": section, "decision": "include", "reason": "담당자 지정"}
    res = llm.review_batch([item], cfg["max_body_chars"], force=True)
    if item["id"] not in res:
        raise SystemExit("AI 요약을 받지 못했습니다. 잠시 후 다시 실행해주세요.")
    item["review"] = res[item["id"]]
    if section:
        item["review"]["section"] = section
        if item["review"]["subsection"] not in llm.SUBSECTIONS[section]:
            item["review"]["subsection"] = llm.SUBSECTIONS[section][0]
    rec = to_record(item)
    rec["tags"].append("수동 추가")

    path = DAYS_DIR / f"{day}.json"
    data = read_json(path, {"date": day, "items": [], "excluded": [], "stats": {}})
    data["items"] = sort_records([r for r in data["items"] if r["url"] != url] + [rec])
    data["excluded"] = [e for e in data.get("excluded", []) if e["url"] != url]
    write_json(path, data)
    update_index()
    log.info("추가 완료: [%s] %s → %s", rec["press"], rec["title"], day)


def remove(url: str) -> None:
    found = False
    for path in DAYS_DIR.glob("*.json"):
        data = read_json(path, {})
        hit = [r for r in data.get("items", []) if r["url"] == url]
        if not hit:
            continue
        found = True
        data["items"] = [r for r in data["items"] if r["url"] != url]
        r = hit[0]
        data.setdefault("excluded", []).append({
            "title": r["title"], "url": url, "press": r["press"], "pub_date": r["pub_date"],
            "section": r["section"], "stage": "담당자", "reason": "담당자 제외"})
        write_json(path, data)
        log.info("제외 완료: %s (%s)", r["title"], path.stem)
    if not found:
        raise SystemExit("해당 링크의 게재 기사를 찾지 못했습니다.")
    update_index()


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["add", "remove"])
    ap.add_argument("url")
    ap.add_argument("--section", choices=list(llm.SUBSECTIONS.keys()), default=None)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 오늘)")
    a = ap.parse_args(argv)
    if a.action == "add":
        add(a.url.strip(), a.section, a.date)
    else:
        remove(a.url.strip())


if __name__ == "__main__":
    main()
