"""AI 오류로 판정을 못 받은 기사 재처리.
그날 'AI 판정 못 함'으로 빠진 기사와 'AI 미판정'(네이버 요약문 그대로)으로 실린 기사를
본문을 다시 받아 정상 판정·요약하고, 결과를 그날 파일에 다시 넣는다.
GitHub Actions의 'Redo failed' 워크플로에서 호출된다."""
from __future__ import annotations

import argparse
import logging

from . import llm
from .common import DAYS_DIR, now_kst, read_json, settings, update_index, url_id, write_json
from .run_daily import (drop_repeats, drop_thin_summaries, fix_summary_length, promote_own, rescue_own,
                        run_review, sort_records, to_record, trim_long_summaries, verify_summaries)

log = logging.getLogger("redo")

_KEYS = ("title", "url", "press", "pub_date", "section")


def _targets(data: dict) -> list[dict]:
    seen, out = set(), []
    pool = [r for r in data.get("items", []) if "AI 미판정" in r.get("tags", [])]
    pool += [e for e in data.get("excluded", []) if e.get("reason", "").startswith("AI 판정 못 함")]
    for r in pool:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        sec = r.get("section") or "fandom_music"
        out.append({"url": r["url"], "naver_url": r["url"] if "naver.com" in r["url"] else None,
                    "title": r["title"], "description": "", "press": r.get("press", ""),
                    "pub_date": r["pub_date"], "lang": r.get("lang", "ko"), "queries": ["재처리"],
                    "section_hints": [sec], "cluster_size": r.get("cluster_size", 1), "id": url_id(r["url"]),
                    "triage": {"section": sec, "decision": "maybe", "reason": "AI 오류로 판정 못 해 재처리"}})
    return out


def redo(day: str) -> None:
    cfg = settings()
    path = DAYS_DIR / f"{day}.json"
    data = read_json(path, None)
    if not data:
        raise SystemExit(f"{day} 파일이 없습니다.")
    items = _targets(data)
    if not items:
        log.info("재처리할 기사가 없습니다.")
        return
    urls = {it["url"] for it in items}
    log.info("재처리 대상 %d건", len(items))

    quota_out = run_review(items, cfg)
    if not quota_out:
        rescue_own(items, cfg)
    promote_own(items)
    _, issues = verify_summaries(items, cfg)
    tried = set()
    if not quota_out:
        tried = fix_summary_length(items, cfg, issues)["tried"]
    verify_summaries(items, cfg)
    trim_long_summaries(items, cfg)
    drop_thin_summaries(items, cfg, tried)

    keep_items = [r for r in data["items"] if r["url"] not in urls]
    excluded = [e for e in data.get("excluded", []) if e["url"] not in urls]
    records = []
    for it in items:
        r = it.get("review")
        if r is None or r.get("no_ai"):
            # 이번에도 AI 판정을 못 받은 기사는 싣지 않음(네이버 요약문 그대로 싣지 않기 위해)
            excluded.append({**{k: it[k] for k in _KEYS if k in it}, "section": it["section_hints"][0],
                             "stage": "2차", "reason": "AI 판정 못 함(재처리도 실패)"})
        elif r.get("keep"):
            records.append(to_record(it))
        else:
            excluded.append({**{k: it[k] for k in _KEYS if k in it}, "section": it["section_hints"][0],
                             "stage": "2차", "reason": r.get("reason", "")})

    # 중복: 최근 며칠 게재분 + 오늘 이미 실린 기사와 같은 사안이면 뺀다
    if records:
        drops = {}
        try:
            recent = [x for p in sorted(DAYS_DIR.glob("*.json"))
                      if p.stem < day for x in read_json(p, {}).get("items", [])][-120:]
            for d in llm.dedupe(records, recent + keep_items):
                drops[d["id"]] = d["same_as"]
        except Exception as e:
            log.error("중복 점검 실패(건너뜀): %s", e)
        drops.update(drop_repeats(records, day))
        for r in records:
            if r["id"] in drops:
                excluded.append({**{k: r[k] for k in _KEYS}, "stage": "중복", "reason": "같은 사안: " + drops[r["id"]][:60]})
        records = [r for r in records if r["id"] not in drops]

    min_imp = cfg.get("min_importance", 3)
    low = [r for r in records if r["importance"] < min_imp and not r.get("must_include")]
    excluded += [{**{k: r[k] for k in _KEYS}, "stage": "선별", "reason": f"중요도 {r['importance']} (기준 {min_imp} 미만)"}
                 for r in low]
    records = [r for r in records if r not in low]
    # 섹션별 하루 한도(예: AI & Tech 4건) — 이미 실린 기사 수를 빼고 남은 만큼만
    sec_caps = cfg.get("section_caps", {})
    records.sort(key=lambda r: -r["importance"])
    capped = []
    for r in records:
        lim = sec_caps.get(r["section"], 0)
        used = sum(1 for x in keep_items + capped if x["section"] == r["section"])
        if lim and used >= lim:
            excluded.append({**{k: r[k] for k in _KEYS}, "stage": "2차", "reason": "섹션 한도 초과"})
        else:
            capped.append(r)
    records = capped
    cap = data.get("cap", cfg.get("cap_base", 30))
    records.sort(key=lambda r: (-r["importance"], 0 if r.get("must_include") else 1, 0 if r["section"] == "own" else 1))
    slots = max(0, cap - len(keep_items))
    take, rest = records[:slots], records[slots:]
    extra = [r for r in rest if r.get("must_include") or r["importance"] >= 5][:cfg.get("must_include_overflow", 5)]
    for r in extra:
        r["tags"].append("상한 초과 게재")
    excluded += [{**{k: r[k] for k in _KEYS}, "stage": "선별", "reason": f"게재 상한 {cap}건"}
                 for r in rest if r not in extra]
    for r in take + extra:
        r["tags"].append("재처리")

    data["items"] = sort_records(keep_items + take + extra)
    data["excluded"] = excluded[-400:]
    data.setdefault("stats", {})["kept"] = len(data["items"])
    data["stats"]["redo"] = {"targets": len(items), "added": len(take) + len(extra),
                             "at": now_kst().isoformat(timespec="minutes")}
    write_json(path, data)
    update_index()
    log.info("재처리 완료: 대상 %d건 → 게재 %d건 (오늘 누적 %d건)", len(items), len(take) + len(extra), len(data["items"]))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 오늘)")
    a = ap.parse_args(argv)
    redo(a.date or now_kst().strftime("%Y-%m-%d"))


if __name__ == "__main__":
    main()
