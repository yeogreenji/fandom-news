"""매일 실행: 수집 → 규칙 필터 → 중복 묶기 → AI 1차 선별 → 본문 수집 → AI 2차 판정·요약 → 저장."""
from __future__ import annotations

import argparse
import collections
import math
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import llm, sources, workdays
from .common import (DAYS_DIR, KST, SECTION_ORDER, STATE_FILE, date_label, load_yaml, now_kst,
                     read_json, settings, update_index, url_id, write_json)
from .fetch import fetch_article
from .press import press_from_domain
from .textutil import (_bigrams, _norm_title, _sim, cluster, detect_series, query_terms,
                       rule_excluded, snippet_has, snippet_has_all, topic_links, topic_tokens)

log = logging.getLogger("monitor")


# ── 1. 수집 ─────────────────────────────────────
def collect(since: datetime, until: datetime, cfg: dict, qcfg: dict) -> tuple[list[dict], dict]:
    by_url: dict[str, dict] = {}
    stats = {"raw": 0, "rule_dropped": 0, "snippet_dropped": 0}

    def add(it: dict, q: str, hint: str):
        u = it["url"]
        if not u or it["pub_date"] > until.isoformat():  # 기준시각 이후 기사는 다음 회차로
            return
        cur = by_url.get(u)
        if cur is None:
            it["queries"], it["section_hints"] = [q], [hint]
            by_url[u] = it
        else:
            if q not in cur["queries"]:
                cur["queries"].append(q)
            if hint not in cur["section_hints"]:
                cur["section_hints"].append(hint)

    for g in qcfg["groups"]:
        for qd in g["queries"]:
            q = qd["q"]
            try:
                found = sources.naver_search(q, since, qd.get("limit") or g.get("limit") or cfg["per_query_limit"])
            except Exception as e:
                log.error("검색 실패 %s: %s", q, e)
                continue
            stats["raw"] += len(found)
            for it in found:
                if rule_excluded(it["title"]):
                    stats["rule_dropped"] += 1
                    continue
                if g.get("snippet_check", True):
                    ok = snippet_has(it, qd["must_any"]) if qd.get("must_any") else snippet_has_all(it, query_terms(q))
                    if not ok:
                        stats["snippet_dropped"] += 1
                        continue
                add(it, q, g["section_hint"])
            log.info("  %-14s %4d건", q, len(found))

    for feed in qcfg.get("rss", []):
        found = sources.rss_fetch(feed["url"], since)
        stats["raw"] += len(found)
        for it in found:
            it["press"] = feed["name"]
            add(it, feed["name"], feed["section_hint"])
        log.info("  RSS %-10s %4d건", feed["name"], len(found))

    return list(by_url.values()), stats


# ── 2. 이전 날짜와 중복 제거 ─────────────────────
def recent_items(days: int = 3) -> list[dict]:
    """최근 며칠(오늘 이미 저장된 분 포함) 게재 기사."""
    out = []
    for p in _day_files()[-days:]:
        out += read_json(p, {}).get("items", [])
    return out


def _day_files(before: str | None = None) -> list:
    """날짜 형식(YYYY-MM-DD) 결과 파일만, 오래된 순. before를 주면 그 날짜보다 앞선 파일만."""
    fs = sorted(p for p in DAYS_DIR.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    return [p for p in fs if before is None or p.stem < before]


def drop_same_day(records: list[dict], need: int = 11) -> dict[str, str]:
    """AI 중복 점검이 놓친 '같은 날 같은 보도자료' 기사를 코드로 묶어 1건만 남긴다(자사 제외).
    제목·요약에서 드문 단어를 need개 이상 공유하면 같은 사안으로 보고, 묶음 안에서
    중요도 높은 기사 → 주요 매체(PRESS_PRIORITY) 순으로 1건을 남긴다.
    반환: {빼는 기사 id: 남기는 기사 제목}"""
    from .press import PRESS_PRIORITY
    recs = [r for r in records if r.get("section") != "own"]
    if len(recs) < 2:
        return {}
    txt = lambda r: r["title"] + " " + (r.get("summary") or "")[:400]
    toks = [topic_tokens(txt(x)) for x in recs]
    df = collections.Counter(w for t in toks for w in t)
    lim = max(3, int(len(toks) * 0.03))
    parent = list(range(len(recs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(recs)):
        for j in range(i + 1, len(recs)):
            if len({w for w in toks[i] & toks[j] if df[w] <= lim}) >= need:
                parent[find(i)] = find(j)
    groups = collections.defaultdict(list)
    for i in range(len(recs)):
        groups[find(i)].append(recs[i])
    rank = lambda r: (-r.get("importance", 3),
                      PRESS_PRIORITY.index(r["press"]) if r.get("press") in PRESS_PRIORITY else 99, r["pub_date"])
    out = {}
    for g in groups.values():
        if len(g) > 1:
            g.sort(key=rank)
            for r in g[1:]:
                out[r["id"]] = g[0]["title"]
    return out


def drop_repeats(records: list[dict], day: str, days: int = 4) -> dict[str, str]:
    """AI 중복 점검이 놓친 '최근 게재분과 같은 사안'을 코드로 한 번 더 거른다.
    같은 보도자료가 수집 기준 시각(09:30) 전후로 나뉘어 이틀 연속 실리는 경우를 막기 위한 안전장치.
    제목·요약에서 드물게 나오는 단어를 많이 공유하면(자사 8개, 그 외 13개 이상) 같은 사안으로 본다.
    반환: {빼는 기사 id: 겹치는 기존 기사 제목}"""
    recent = [x for p in _day_files(before=day)[-days:] for x in read_json(p, {}).get("items", [])]
    if not recent or not records:
        return {}
    txt = lambda r: r["title"] + " " + (r.get("summary") or "")[:400]
    toks = [topic_tokens(txt(x)) for x in records + recent]
    df = collections.Counter(w for t in toks for w in t)
    lim = max(3, int(len(toks) * 0.03))
    urls = {x["url"]: x["title"] for x in recent}
    out = {}
    for i, r in enumerate(records):
        if r["url"] in urls:
            out[r["id"]] = urls[r["url"]]
            continue
        need = 8 if r.get("section") == "own" else 13
        for j, p in enumerate(recent):
            if len({w for w in toks[i] & toks[len(records) + j] if df[w] <= lim}) >= need:
                out[r["id"]] = p["title"]
                break
        else:
            # 자사: 최근 자사 기사 제목의 고유명사(아티스트·고객사·행사명)가 이번 기사에 나오면 같은 사안으로 본다
            # 예) 10/23 “신원호, 비스테이지 공식 팬 커뮤니티 오픈” → 10/24 “비마이프렌즈∙아뮤즈 첫 협업”(본문에 신원호)
            if r.get("section") == "own":
                for j, p in enumerate(recent):
                    if p.get("section") != "own":
                        continue
                    names = {w for w in topic_tokens(p["title"]) if len(w) >= 2 and not w.isdigit()
                             and w not in _OWN_GENERIC and df[w] <= 3}
                    if names & toks[i]:
                        out[r["id"]] = p["title"]
                        break
    return out


# 자사 고유명사 비교에서 빼는 일반어(자사 이름·서비스명·흔한 단어)
_OWN_GENERIC = {"비스테이지", "플로", "flo", "비마이프렌즈", "드림어스", "드림어스컴퍼니", "stage", "서우석", "이기영",
                "플로집", "팬덤", "플랫폼", "글로벌", "공식", "커뮤니티", "오픈", "응원하기", "가수", "배우", "연결",
                "팬클럽", "비즈니스", "재생", "콘텐츠", "아티스트", "음원", "음악", "멤버십", "협업", "출시"}


def recent_signatures(days: int = 4) -> list[set]:
    return [_bigrams(_norm_title(it["title"])) for it in recent_items(days)]


def drop_seen(items: list[dict], seen: dict) -> list[dict]:
    sigs = recent_signatures()
    out = []
    for it in items:
        if it["url"] in seen:
            continue
        s = _bigrams(_norm_title(it["title"]))
        if any(_sim(s, x) >= 0.45 for x in sigs):
            continue
        out.append(it)
    return out


# ── 3. AI 판정 ──────────────────────────────────
def _fallback_triage(it: dict, reason: str) -> dict:
    return {"decision": "maybe", "section": it["section_hints"][0], "importance": 2, "reason": reason}


SEC_RANK = {"own": 0, "fandom_music": 1, "ent_content": 2, "ai_tech": 3}


def prerank(items: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """AI 호출 수를 줄이려고 1차 선별 전에 후보를 추림.
    섹션 우선순위(자사>팬덤·음악>엔터>AI) → 제목에 검색어 포함 → 동일 보도 많은 순, 섹션별 상한 적용."""
    def sec(it):
        return min(it["section_hints"], key=lambda h: SEC_RANK.get(h, 9))

    def title_hit(it):
        t = it["title"].replace(" ", "").lower()
        return any(all(w.lower() in t for w in q.split()) for q in it.get("queries", []))

    caps = cfg.get("triage_caps", {})
    keep, drop = [], []
    for s_name in sorted(SEC_RANK, key=SEC_RANK.get):
        group = [it for it in items if sec(it) == s_name]
        group.sort(key=lambda it: (0 if title_hit(it) else 1, -it.get("cluster_size", 1), it["pub_date"]))
        n = caps.get(s_name, 0) or len(group)
        keep += group[:n]
        drop += group[n:]
    # 핵심 경쟁·유통 플랫폼 키워드 기사는 상한에 밀려도 되살린다(제목에 키워드가 있는 기사부터)
    pri = [it for it in drop if priority_hit(it, cfg)]
    pri.sort(key=lambda it: (0 if priority_hit(it, cfg, title_only=True) else 1, it["pub_date"]))
    pri = pri[:cfg.get("priority_max", 80)]
    ids = {it["id"] for it in pri}
    if pri:
        log.info("핵심 키워드 기사 %d건을 1차 선별에 추가", len(pri))
    return keep + pri, [it for it in drop if it["id"] not in ids]


def priority_hit(it: dict, cfg: dict, title_only: bool = False) -> bool:
    """제목(또는 제목+요약문)에 핵심 경쟁·유통 플랫폼 키워드(settings의 priority_terms)가 있는지."""
    text = it.get("title", "") + ("" if title_only else " " + (it.get("description") or ""))
    text = text.replace(" ", "").lower()
    return any(t.replace(" ", "").lower() in text for t in cfg.get("priority_terms", []))


def run_triage(items: list[dict], cfg: dict) -> None:
    """묶음 단위로 순차 호출(무료 등급 분당 제한 대응). 한도 소진 시 나머지는 AI 없이 보류 처리.
    응답이 잘려 해석에 실패하면 묶음을 반으로 나눠 한 번 더 시도."""
    bs = cfg["triage_batch_size"]
    quota_out = False

    def ask(batch, depth=0):
        nonlocal quota_out
        if quota_out or not batch:
            return {}
        try:
            return llm.triage(batch)
        except llm.QuotaExceeded as e:
            log.error("AI 한도 소진(1차): %s", e)
            quota_out = True
            return {}
        except Exception as e:
            log.error("1차 선별 실패(%d건): %s", len(batch), e)
            if depth == 0 and len(batch) > 10:
                half = len(batch) // 2
                return {**ask(batch[:half], 1), **ask(batch[half:], 1)}
            return {}

    for i in range(0, len(items), bs):
        batch = items[i:i + bs]
        res = ask(batch)
        for it in batch:
            r = res.get(it["id"])
            if r:
                r["ai"] = True
                it["triage"] = r
            else:
                it["triage"] = _fallback_triage(it, "AI 한도" if quota_out else "AI 응답 없음")
            it["triage"].setdefault("importance", 2)


_STOCK_TITLE_RE = re.compile(r"특징주|관련주|테마주|엔터주|음악주|게임주|상한가|하한가|목표주가|목표가|주가\S*\s*(급등|급락|강세|약세)")


def _topic_prio(cands: list[dict]) -> None:
    """같은 사안을 여러 매체(4곳 이상)가 다룬 '큰 이슈'는 검토 우선순위를 1 올리고(최대 4),
    같은 사안의 두 번째 기사부터는 'mate'로 표시해 검토 순서를 뒤로 미룬다(대표 1건이 먼저 검토되도록)."""
    texts = [it["title"] + " " + (it.get("description") or "")[:80] for it in cands]
    heat = topic_links(texts, k=2, max_df_ratio=0.15)     # 보도량 집계용(느슨하게)
    same = topic_links(texts, k=3, max_df_ratio=0.15)     # 같은 사안 판정용(엄격하게)
    for i, it in enumerate(cands):
        t = it["triage"]
        n = 1 + len(heat[i]) + sum(cands[j].get("cluster_size", 1) - 1 for j in heat[i]) + it.get("cluster_size", 1) - 1
        t["topic_size"] = n
        imp = t.get("importance", 2)
        t["prio"] = max(imp, min(4, imp + 1)) if n >= 4 else imp
    return same


def pick_for_review(items: list[dict], cap: int, reserve: dict | None = None,
                    cfg: dict | None = None) -> tuple[list[dict], list[dict]]:
    # AI가 실제로 판단한 '포함/보류'만 본문 검토로. AI 판단을 못 받은 기사는 자사만 예외적으로 포함
    # 단순 주가 기사(제목에 특징주·강세 등)는 본문 검토 전에 뺀다
    cands = [it for it in items if it["triage"]["decision"] != "exclude"
             and (it["triage"].get("ai") or "own" in it["section_hints"])
             and (not _STOCK_TITLE_RE.search(it.get("title", "")) or (cfg and priority_hit(it, cfg, title_only=True)))]
    same = _topic_prio(cands) if cands else []

    def key(it):
        t = it["triage"]
        own = 0 if ("own" in it["section_hints"] or t["section"] == "own") else (1 if cfg and priority_hit(it, cfg) else 2)
        return (own, -t.get("prio", t.get("importance", 2)), 0 if t["decision"] == "include" else 1,
                -t.get("topic_size", 1))

    order = sorted(range(len(cands)), key=lambda i: key(cands[i]))
    # 같은 사안의 대표(먼저 오는 기사) 1건만 앞에 두고 나머지는 맨 뒤로
    rep_idx: list[int] = []
    reps, mates = [], []
    for i in order:
        hit = [r for r in rep_idx if same and r in same[i]]
        if hit and "own" not in cands[i]["section_hints"]:
            cands[i]["triage"]["mate"] = True
            cands[i]["triage"]["rep_id"] = cands[hit[0]]["id"]
            mates.append(cands[i])
        else:
            reps.append(cands[i])
            rep_idx.append(i)
    cands = reps + mates
    head, rest = cands[:cap], cands[cap:]
    # 섹션별 최소 검토 건수 보장(예: AI 섹션이 다른 섹션에 밀려 통째로 빠지지 않게)
    for sec, n in (reserve or {}).items():
        have = sum(1 for it in head if it["triage"].get("section") == sec)
        add = [it for it in rest if it["triage"].get("section") == sec and not it["triage"].get("mate")][:max(0, n - have)]
        if not add:
            continue
        protected = set(reserve)
        for it in add:
            # 뒤에서부터, 자사가 아니고 보장 대상 섹션도 아닌 기사와 맞바꿈
            for j in range(len(head) - 1, -1, -1):
                h = head[j]
                if h["triage"].get("section") not in protected and "own" not in h["section_hints"]:
                    rest.append(head.pop(j))
                    break
            head.append(it)
            rest.remove(it)
    rest_reps = sorted([it for it in rest if not it["triage"].get("mate")], key=key)
    rest = rest_reps + [it for it in rest if it["triage"].get("mate")]
    return head, rest


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def run_review(items: list[dict], cfg: dict) -> bool:
    """본문 판정·요약. 오류로 판정을 못 받은 기사는 작은 묶음으로 한 번 더 시도한다.
    반환값: AI 한도·시간 예산 소진 여부"""
    todo = [it for it in items if not it.get("body_fetched")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(fetch_article, todo))
    for it in todo:
        it["body_fetched"] = True
    # 본문이 너무 짧아 요약할 내용이 없는 기사는 AI에 보내지 않고 제외(자사 기사는 예외)
    min_body = cfg.get("min_body_chars", 0)
    if min_body:
        thin = [it for it in items if not _is_own(it) and len(it.get("body") or "") < min_body]
        for it in thin:
            it["review"] = {"keep": False, "reason": f"본문 내용 부족({len(it.get('body') or '')}자)", "thin": True}
        if thin:
            log.info("본문 부족 %d건 제외(기준 %d자)", len(thin), min_body)
        items = [it for it in items if not (it.get("review") or {}).get("thin")]
    state = {"quota_out": False}

    def ask(batch):
        if state["quota_out"]:
            return {}
        try:
            return llm.review_batch(batch, cfg["max_body_chars"])
        except llm.QuotaExceeded as e:
            log.error("AI 한도 소진(2차): %s", e)
            state["quota_out"] = True
        except Exception as e:
            log.error("2차 판정 실패(%d건): %s", len(batch), e)
        return {}

    failed = []
    for batch in _chunks(items, cfg["review_batch_size"]):
        res = ask(batch)
        for it in batch:
            it["review"] = res.get(it["id"])
            if it["review"] is None:
                failed.append(it)
    # 서버 과부하(503) 등으로 빠진 기사 재시도: 잠깐 쉬고 3건씩
    if failed and not state["quota_out"]:
        log.info("2차 판정 실패 %d건 재시도", len(failed))
        time.sleep(20)
        still = []
        for batch in _chunks(failed, 3):
            res = ask(batch)
            for it in batch:
                it["review"] = res.get(it["id"])
                if it["review"] is None:
                    still.append(it)
        failed = still
    for it in failed:
        it["review"] = no_ai_review(it)
    return state["quota_out"]


# ── 요약 사실 검증: 기사 본문에 근거가 없는 문장은 지운다 ──
# 회사 관계(자회사 등)·평가·전망 표현은 본문에 같은 말이 있을 때만 허용
_RISK_TERMS = ["자회사", "모회사", "계열사", "손자회사", "관계사", "산하", "지분", "인수", "합병",
               "전망", "기대된다", "평가할", "평가된다", "시사점", "풀이된다", "분석된다", "관측된다",
               "주목받", "본격적", "한층", "청신호", "발판", "입지를", "의미가 크", "의미 있는"]
_SENT_RE = re.compile(r".+?[.!?](?:\*\*)?(?=\s+|$)", re.S)


def _gram2(text: str) -> set:
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _norm_num(text: str) -> str:
    return re.sub(r"(?<=\d),(?=\d)", "", text)


def check_sentence(sent: str, ref: str, ref_grams: set, cfg: dict) -> str:
    """문제 없으면 "", 있으면 사유."""
    plain = sent.replace("**", "").strip()
    for term in _RISK_TERMS:
        if term in plain and term not in ref:
            return f"본문에 없는 표현 '{term}'"
    for num in re.findall(r"\d+(?:\.\d+)?", _norm_num(plain)):
        if len(num) >= 2 and num not in ref:
            return f"본문에 없는 숫자 {num}"
    g = _gram2(plain)
    if len(g) >= 12:
        cov = len(g & ref_grams) / len(g)
        if cov < cfg.get("ground_min_coverage", 0.55):
            return f"본문과 겹치는 표현이 적음({cov:.0%})"
    return ""


def verify_summaries(items: list[dict], cfg: dict) -> tuple[int, dict]:
    """게재 후보 요약을 문장 단위로 본문과 대조해 근거 없는 문장을 지운다. (영문 기사는 맥락 문장이 있어 건너뜀)"""
    removed, issues = 0, {}
    for it in items:
        r = it.get("review") or {}
        if not r.get("keep") or not r.get("summary") or it.get("lang", "ko") != "ko":
            continue
        ref = _norm_num(" ".join([it.get("title", ""), it.get("description", ""), it.get("body", "")]))
        if len(ref) < 100:
            continue
        grams = _gram2(ref)
        text = r["summary"].strip()
        ms = list(_SENT_RE.finditer(text))
        sents = [m.group().strip() for m in ms if m.group().strip()]
        tail = text[ms[-1].end():].strip() if ms else text
        if tail:
            sents.append(tail)
        keep, bad = [], []
        for sent in sents:
            why = check_sentence(sent, ref, grams, cfg)
            (bad if why else keep).append((sent, why))
        if not bad:
            continue
        if not keep:  # 전부 걸리면 판단 보류(본문 추출 실패 가능성) — 그대로 둠
            continue
        r["summary"] = " ".join(x.strip() for x, _ in keep)
        issues[it["id"]] = [f"{why}: {x.strip()[:60]}" for x, why in bad]
        removed += len(bad)
        log.info("근거 없는 문장 %d개 삭제 [%s] %s", len(bad), it.get("title", "")[:30], "; ".join(w for _, w in bad))
    return removed, issues


# 자사 이름이 요약·제목에 나오면 어느 키워드로 수집됐든 자사 섹션으로 옮긴다(주가 기사의 종목 나열은 제외)
_STOCK_RE = re.compile(r"관련주|특징주|엔터주|테마주|주가|매수세|목표주가|상한가|하한가|급등|급락")
_OWN_TERMS = ["비마이프렌즈", "서우석", "비스테이지", "b.stage", "bemyfriends", "드림어스", "플로집", "Dreamus"]
# 드림어스가 투자한 기획사·아티스트(기준 문서 '자사' 범위). 기사에 관계 표현이 없어도 섹션만 자사로 옮긴다
_OWN_RELATED_TERMS = ["타이탄콘텐츠", "타이탄뮤직", "앳하트", "AtHeart", "알앤디컴퍼니"]


def promote_own(items: list[dict]) -> int:
    moved = 0
    for it in items:
        r = it.get("review") or {}
        if not r.get("keep") or r.get("section") == "own":
            continue
        text = (it.get("title", "") + " " + (r.get("summary") or "")).lower()
        terms = _OWN_TERMS + _OWN_RELATED_TERMS
        if any(t.lower() in text for t in terms) and not _STOCK_RE.search(text):
            r["section"], r["subsection"] = "own", "own"
            r["importance"] = max(r.get("importance", 3), 4)
            if "own" not in it.get("section_hints", []):
                it.setdefault("section_hints", []).append("own")
            moved += 1
            log.info("자사 섹션으로 이동: %s", it.get("title", "")[:40])
    return moved


# 1차·2차에서 자사 언급 기사가 '단순 언급'으로 빠지지 않게 하는 판별(플로는 조사까지 허용해 플로리다·플로어 등은 제외)
_OWN_MENTION_RE = re.compile(
    r"비마이프렌즈|서우석|비스테이지|b\.stage|bemyfriends|드림어스|플로집|dreamus|\bFLO\b"
    r"|(?<![가-힣A-Za-z])플로(?=$|[^가-힣]|[가는은의를을에와과도로만까])", re.I)


# 음원 차트·순위·브랜드평판 기사(본문에 플로가 집계 플랫폼으로만 나오는 경우) 판별
_RANK_TITLE_RE = re.compile(r"\d+\s*위|차트|순위|뮤직100|가수브랜드|브랜드평판|계단\s*(상승|하락)|\d+주차")


def _own_mention(it: dict) -> bool:
    text = it.get("title", "") + " " + (it.get("description") or "") + " " + (it.get("body") or "")[:4000]
    return bool(_OWN_MENTION_RE.search(text)) and not _STOCK_TITLE_RE.search(it.get("title", ""))


def protect_own(items: list[dict]) -> int:
    """1차에서 자사 언급 기사가 exclude되면 maybe·자사로 되돌린다(기준: 자사는 단순 언급도 포함)."""
    n = 0
    for it in items:
        t = it.get("triage") or {}
        if t.get("decision") == "exclude" and _own_mention(it):
            t.update(decision="maybe", section="own", importance=max(t.get("importance", 2), 4),
                     reason="자사 언급(단순 언급도 포함 기준)")
            if "own" not in it.setdefault("section_hints", []):
                it["section_hints"].append("own")
            n += 1
            log.info("자사 언급 1차 제외 취소: %s", it.get("title", "")[:40])
    return n


def rescue_own(items: list[dict], cfg: dict) -> int:
    """2차에서 자사 언급 기사가 빠졌으면 '자사 언급' 안내를 붙여 한 번 더 판정(이벤트 공지·주가 기사는 그대로 제외)."""
    cands = [it for it in items if (r := it.get("review")) is not None and not r.get("keep") and _own_mention(it)
             and not (_RANK_TITLE_RE.search(it.get("title", "")) and not _OWN_MENTION_RE.search(it.get("title", "")))]
    if not cands:
        return 0
    note = ("이 기사들에는 자사(비마이프렌즈·비스테이지·드림어스컴퍼니·플로)가 나온다. 기준상 자사는 단순 언급이라도 "
            "주가·증권 기사나 경품·응모·할인 이벤트 공지가 아니면 포함한다. 해당하면 keep=true, section=own으로 두고 "
            "기사 내용을 요약하되 자사가 나오는 문장을 반드시 넣고 **굵게** 표시한다. 이벤트 공지·주가 기사면 keep=false를 유지한다.")
    n = 0
    for batch in _chunks(cands, 4):
        try:
            res = llm.review_batch(batch, cfg["max_body_chars"], note=note)
        except Exception as e:
            log.error("자사 언급 재판정 실패(%d건): %s", len(batch), e)
            continue
        for it in batch:
            r = res.get(it["id"])
            if r and r.get("keep"):
                it["review"] = r
                n += 1
                log.info("자사 언급 기사 재판정 후 포함: %s", it.get("title", "")[:40])
    return n


def _is_own(it: dict) -> bool:
    t = it.get("triage") or {}
    return "own" in it.get("section_hints", []) or t.get("section") == "own"


def _len_range(kind: str, cfg: dict) -> tuple[int, int]:
    lens = cfg.get("summary_len", {"news": [440, 500], "long": [550, 650]})
    return tuple(lens["long"] if kind in ("trend", "column", "interview") else lens["news"])


def fix_summary_length(items: list[dict], cfg: dict, issues: dict | None = None) -> dict:
    """요약이 목표 분량을 벗어나면 한 번 다시 쓰게 함(판정은 그대로, 요약만 교체).
    issues: 사실 검증에서 지운 문장 사유(다시 쓸 때 같은 실수를 하지 않도록 알려줌)."""
    issues = issues or {}
    tol = cfg.get("summary_len_tolerance", 20)
    def off(it):
        r = it["review"]
        lo, hi = _len_range(r.get("kind", "news"), cfg)
        n = len((r.get("summary") or "").strip())
        return n < lo - tol or n > hi + tol or bool(re.search(r"습니다|니다\.|어요\.|에요\.", r.get("summary") or ""))
    targets = [it for it in items
               if (r := it.get("review")) and r.get("keep") and not r.get("no_ai")
               and r.get("kind") != "disclosure" and off(it)]
    fixed, tried, dropped = 0, set(), 0
    for batch in _chunks(targets, 3):
        guide = "; ".join(
            f"id {it['id']}: 지금 {len(it['review'].get('summary') or '')}자 → 목표 {_len_range(it['review'].get('kind','news'), cfg)[0]}~{_len_range(it['review'].get('kind','news'), cfg)[1]}자"
            + (f" (본문 근거가 없어 지운 문장: {' / '.join(issues[it['id']])} — 이런 내용은 다시 쓰지 말 것)" if it["id"] in issues else "")
            for it in batch)
        try:
            res = llm.review_batch(batch, cfg["max_body_chars"], task="rewrite",
                                   note="앞서 쓴 요약의 분량이나 문체(~했다 평서문)가 기준을 벗어났다. keep·섹션·중요도 판단은 그대로 두고 summary만 "
                                        "목표 글자 수에 맞춰 다시 쓴다. 본문에 있는 사실만 쓰고, 분량을 채우려고 평가·전망·의미 부여 "
                                        "문장을 덧붙이지 않는다. 회사 간 관계(자회사·계열사·투자 등)는 본문에 그 표현이 있을 때만 쓴다. "
                                        "본문 사실이 모자라면 목표보다 짧아도 된다. " + guide)
        except llm.QuotaExceeded:
            break
        except Exception as e:
            log.error("요약 분량 보정 실패(%d건): %s", len(batch), e)
            continue
        for it in batch:
            tried.add(it["id"])
            new = ((res.get(it["id"]) or {}).get("summary") or "").strip()
            if not new:
                continue
            lo, hi = _len_range(it["review"].get("kind", "news"), cfg)
            mid = (lo + hi) / 2
            old = (it["review"].get("summary") or "").strip()
            if abs(len(new) - mid) < abs(len(old) - mid):
                it["review"]["summary"] = new
                fixed += 1
    if targets:
        log.info("분량 벗어난 요약 %d건 중 %d건 보정", len(targets), fixed)
    return {"fixed": fixed, "targets": len(targets), "tried": tried}


def drop_thin_summaries(items: list[dict], cfg: dict, tried: set) -> int:
    """보정을 시도했는데도 요약이 이 글자 수보다 짧으면 '요약할 내용 부족'으로 제외(자사·꼭 실을 기사 예외).
    기사에 없는 말로 분량을 채우지 않기 때문에, 내용이 조금 적은 기사는 짧은 요약으로 그대로 싣는다."""
    floor, dropped = cfg.get("summary_drop_below", 250), 0
    for it in items:
        r = it.get("review") or {}
        if not r.get("keep") or it["id"] not in tried or _is_own(it) or r.get("must_include"):
            continue
        n = len((r.get("summary") or "").strip())
        if n < floor:
            r["keep"] = False
            r["reason"] = f"요약할 내용 부족(요약 {n}자)"
            dropped += 1
    return dropped


def trim_long_summaries(items: list[dict], cfg: dict) -> int:
    """다시 써도 목표보다 길면 뒤 문장부터 잘라 상한에 맞춤(요약은 중요한 내용이 앞에 오므로)."""
    tol, trimmed = cfg.get("summary_len_tolerance", 20), 0
    for it in items:
        r = it.get("review") or {}
        if not r.get("keep") or not r.get("summary") or r.get("kind") == "disclosure":
            continue
        _, hi = _len_range(r.get("kind", "news"), cfg)
        text = r["summary"].strip()
        if len(text) <= hi + tol:
            continue
        sents = [m.group().strip() for m in _SENT_RE.finditer(text)]
        if len(sents) < 5:
            continue
        while len(sents) > 4 and len(" ".join(sents)) > hi + tol:
            sents.pop()
        r["summary"] = " ".join(sents)
        trimmed += 1
    return trimmed


def no_ai_review(it: dict) -> dict | None:
    """AI 본문 판정을 못 받은 경우: 자사 기사만 네이버 요약문으로 게재(다른 기사는 품질 때문에 싣지 않음)."""
    t = it.get("triage", {})
    own = _is_own(it)
    if not own:
        return None
    sec = "own" if own else t.get("section", it["section_hints"][0])
    return {"keep": True, "reason": "AI 미판정", "section": sec, "subsection": llm.SUBSECTIONS[sec][0],
            "importance": t.get("importance", 2), "kind": "news", "press": it.get("press", ""),
            "summary": it.get("description", ""), "no_ai": True}


# ── 존댓말 어미('~습니다') → 기사체 평서문('~다') ──
_ODD = {"입니다": "이다", "아닙니다": "아니다", "있습니다": "있다", "없습니다": "없다"}


def _ending(m: re.Match) -> str:
    w = m.group(1)
    for k, v in _ODD.items():
        if w.endswith(k):
            return w[:-len(k)] + v
    if w.endswith("습니다"):
        return w[:-3] + "다"
    # 'ㅂ니다'(됩니다·합니다·갑니다) → 받침 ㅂ을 ㄴ으로: 된다·한다·간다
    stem = w[:-2]
    last = stem[-1]
    code = ord(last) - 0xAC00
    if 0 <= code < 11172 and code % 28 == 17:
        return stem[:-1] + chr(ord(last) - 17 + 4) + "다"
    return w


def plain_style(text: str) -> str:
    return re.sub(r"([가-힣]*(?:습니다|니다))(?=[.!?]|\s|$|\*)", _ending, text or "")


# ── 4. 결과 정리 ────────────────────────────────
def to_record(it: dict) -> dict:
    r = it["review"]
    pub = datetime.fromisoformat(it["pub_date"])
    tags = []
    if r["section"] == "own" and r.get("press_release_guess"):
        tags.append("자사 보도자료 추정")
    if it.get("lang") == "en":
        tags.append("영문")
    if r.get("kind") == "interview":
        tags.append("인터뷰")
    if it.get("cluster_size", 1) >= 3:
        tags.append(f"동일 보도 {it['cluster_size']}건")
    if r.get("no_ai"):
        tags.append("AI 미판정")
    elif not it.get("body_ok"):
        tags.append("본문 미확보")
    sub = r["subsection"] if r["subsection"] in llm.SUBSECTIONS[r["section"]] else llm.SUBSECTIONS[r["section"]][0]
    return {
        "id": it["id"], "title": it["title"], "url": it["url"],
        # 언론사명: 도메인 매핑 → 네이버 표기 → AI 추정 순(AI가 IT동아를 동아일보로 쓰는 식의 오류 방지)
        "press": (press_from_domain(it["url"]) or it.get("press") or r.get("press") or "").strip(),
        "pub_date": it["pub_date"], "date_label": date_label(pub),
        "section": r["section"], "subsection": sub,
        "importance": r.get("importance", 3), "kind": r.get("kind", "news"),
        "must_include": bool(r.get("must_include")),
        "summary": plain_style((r.get("summary") or "").strip()), "reason": r.get("reason", ""),
        "tags": tags, "series": detect_series(it["title"]), "lang": it.get("lang", "ko"),
        "cluster_size": it.get("cluster_size", 1),
    }


def excluded_record(it: dict, stage: str, reason: str) -> dict:
    return {"title": it["title"], "url": it["url"], "press": it.get("press", ""),
            "pub_date": it["pub_date"], "stage": stage, "reason": reason,
            "section": (it.get("triage") or {}).get("section") or it["section_hints"][0]}


def apply_caps(records: list[dict], caps: dict) -> tuple[list[dict], list[dict]]:
    keep, cut = [], []
    for sec in SECTION_ORDER:
        rs = sorted([r for r in records if r["section"] == sec], key=lambda r: -r["importance"])
        n = caps.get(sec, 0) or len(rs)
        keep += rs[:n]
        cut += rs[n:]
    return keep, cut


def sort_records(records: list[dict]) -> list[dict]:
    def key(r):
        subs = llm.SUBSECTIONS[r["section"]]
        return (SECTION_ORDER.index(r["section"]), subs.index(r["subsection"]), -r["importance"], r["pub_date"])
    return sorted(records, key=key)


# ── main ────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, help="수집 범위를 직접 지정(시간, 지금 기준)")
    ap.add_argument("--force", action="store_true", help="휴일이어도 실행")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    cfg, qcfg = settings(), load_yaml("queries.yaml")
    state = read_json(STATE_FILE, {"last_run": None, "seen": {}})
    now = now_kst()
    today = now.date()
    if not (args.hours or args.force) and not workdays.is_workday(today, cfg):
        log.info("오늘(%s)은 쉬는 날(%s) — 건너뜀. 다음 영업일에 묶어서 수집합니다.",
                 today, workdays.holiday_name(today) or "주말/지정 휴무")
        return
    if args.hours:
        since, until = now - timedelta(hours=args.hours), now
    else:
        since, until = workdays.window(now, cfg, state.get("last_window_end"))
        # 자동 실행은 하루 여러 번 예약돼 있음(GitHub 예약 누락 대비) → 오늘 분이 이미 끝났으면 건너뜀
        le = state.get("last_window_end")
        if le and datetime.fromisoformat(le) >= until:
            log.info("오늘(%s) 분은 이미 처리됨 — 건너뜀", until.strftime("%m-%d %H:%M"))
            return
    cap, review_cap = workdays.caps(since, until, cfg)
    log.info("수집 범위: %s ~ %s (%d일치) · 게재 상한 %d건",
             since.strftime("%m-%d %H:%M"), until.strftime("%m-%d %H:%M"),
             workdays.days_covered(since, until), cap)

    started = time.time()
    items, stats = collect(since, until, cfg, qcfg)
    stats["collect_sec"] = round(time.time() - started)
    # AI 처리 시간 예산: 넘으면 그때까지 결과로 마무리(GitHub 40분 제한 대비)
    budget = cfg.get("ai_time_budget_min", 25) * 60
    ai_start = time.time()
    llm.DEADLINE[0] = ai_start + budget * 0.55   # 1차 선별은 예산의 55%까지만
    stats["unique"] = len(items)
    # 수동 실행(--hours)은 '다시 모으기'라 이전 처리 기록은 무시(이미 게재된 기사와의 중복만 제외)
    items = drop_seen(items, {} if args.hours else state["seen"])
    stats["new"] = len(items)
    items = cluster(items)
    stats["clusters"] = len(items)
    for it in items:
        it["id"] = url_id(it["url"])
    log.info("수집 %d → 신규 %d → 중복 묶음 후 %d", stats["unique"], stats["new"], stats["clusters"])

    excluded: list[dict] = []
    if items:
        items, pre_dropped = prerank(items, cfg)
        stats["triage_candidates"] = len(items)
        log.info("1차 선별 대상 %d건 (사전 제외 %d건)", len(items), len(pre_dropped))
        run_triage(items, cfg)
        stats["own_protected"] = protect_own(items)
        to_review, overflow = pick_for_review(items, review_cap, cfg.get("review_reserve", {}), cfg)
        for it in items:
            t = it["triage"]
            if t["decision"] == "exclude" and ("own" in it["section_hints"] or t.get("importance", 1) >= 3):
                excluded.append(excluded_record(it, "1차", t.get("reason", "")))
        log.info("1차 통과 %d건 (한도 초과 %d건)", len(to_review), len(overflow))
        llm.DEADLINE[0] = ai_start + budget           # 남은 시간은 본문 판정·요약에
        stats["triage_sec"] = round(time.time() - ai_start)
        quota_out = run_review(to_review, cfg)
        # 같은 사안 대표 기사가 본문 부족·판정 실패로 빠졌으면 같은 사안의 다른 기사 1건을 대신 검토
        if not quota_out and overflow:
            failed = {it["id"] for it in to_review
                      if (r := it.get("review")) is None or (not r.get("keep") and ("부족" in r.get("reason", "") or r.get("thin")))}
            sub, used = [], set()
            for it in overflow:
                rid = it["triage"].get("rep_id")
                if rid in failed and rid not in used:
                    sub.append(it)
                    used.add(rid)
            if sub:
                ids = {it["id"] for it in sub}
                overflow = [it for it in overflow if it["id"] not in ids]
                log.info("대표 기사 대신 같은 사안 기사 %d건 검토", len(sub))
                quota_out = run_review(sub, cfg)
                to_review += sub
        # 게재 가능 건수가 상한보다 적으면 검토 대기(한도 초과) 기사를 이어서 검토
        min_imp = cfg.get("min_importance", 3)
        for rnd in range(cfg.get("backfill_rounds", 2)):
            ok = sum(1 for it in to_review if (r := it.get("review")) and r.get("keep")
                     and r.get("importance", 0) >= min_imp)
            if quota_out or not overflow or ok >= cap:
                break
            n = min(len(overflow), max(6, math.ceil((cap - ok) * 1.5)))
            more, overflow = overflow[:n], overflow[n:]
            log.info("추가 검토 %d회차: 게재 가능 %d건 < 상한 %d건 → %d건 더 검토", rnd + 1, ok, cap, len(more))
            quota_out = run_review(more, cfg)
            to_review += more
            stats["backfill"] = stats.get("backfill", 0) + len(more)
        # 1차에서 중요도가 높게 나온 후보는 게재 건수와 관계없이 본문 검토(상한 넘어도 실을 기사 찾기)
        pmax = cfg.get("priority_review_max", 12)
        if not quota_out and overflow and pmax:
            pri = [it for it in overflow if it["triage"].get("prio", it["triage"].get("importance", 0)) >= 4
                   and not it["triage"].get("mate")][:pmax]
            if pri:
                ids = {it["id"] for it in pri}
                overflow = [it for it in overflow if it["id"] not in ids]
                log.info("중요 후보 %d건 추가 검토", len(pri))
                quota_out = run_review(pri, cfg)
                to_review += pri
                stats["priority_review"] = len(pri)
        excluded += [excluded_record(it, "1차", "같은 사안 대표 기사로 검토" if it["triage"].get("mate") else "검토 한도 초과")
                     for it in overflow]
        if not quota_out:
            stats["own_rescued"] = rescue_own(to_review, cfg)
        stats["own_promoted"] = promote_own(to_review)
        # 요약 사실 검증 → 분량 보정 → 다시 검증 → 내용 부족 제외
        n1, issues = verify_summaries(to_review, cfg)
        tried = set()
        if not quota_out:
            res = fix_summary_length(to_review, cfg, issues)
            stats["summary_fixed"], tried = res["fixed"], res["tried"]
        n2, _ = verify_summaries(to_review, cfg)
        stats["summary_trimmed"] = trim_long_summaries(to_review, cfg)
        stats["unsupported_removed"] = n1 + n2
        stats["summary_thin_dropped"] = drop_thin_summaries(to_review, cfg, tried)
        stats["ai_sec"] = round(time.time() - ai_start)
    else:
        to_review = []

    records = []
    for it in to_review:
        r = it.get("review")
        if r is None:
            excluded.append(excluded_record(it, "2차", "AI 판정 못 함(한도·오류)"))
        elif r.get("keep"):
            records.append(to_record(it))
        else:
            excluded.append(excluded_record(it, "2차", r.get("reason", "")))
    # 전날·당일 게재분 및 오늘 후보끼리 같은 사안 제거 (AI 1회 호출)
    if records:
        try:
            texts = [r["title"] + " " + r["summary"][:300] for r in records]
            links = topic_links(texts, k=6, max_df_ratio=0.1)
            hints = sorted({(min(i, j), max(i, j)) for i, ls in enumerate(links) for j in ls})
            drops = llm.dedupe(records, recent_items(3), [(records[i]["id"], records[j]["id"]) for i, j in hints])
        except Exception as e:
            log.error("중복 점검 실패(건너뜀): %s", e)
            drops = []
        drop_ids = {d["id"]: d["same_as"] for d in drops}
        for r in records:
            if r["id"] in drop_ids:
                excluded.append({**{k: r[k] for k in ("title", "url", "press", "pub_date", "section")},
                                 "stage": "중복", "reason": "같은 사안: " + drop_ids[r["id"]][:60]})
        records = [r for r in records if r["id"] not in drop_ids]
        log.info("중복 점검: %d건 제외", len(drop_ids))
        # AI가 놓친 최근 게재분과의 중복을 코드로 한 번 더(같은 보도자료가 이틀에 걸쳐 실리는 것 방지)
        rep_ids = drop_repeats(records, until.strftime("%Y-%m-%d"))
        for r in records:
            if r["id"] in rep_ids:
                excluded.append({**{k: r[k] for k in ("title", "url", "press", "pub_date", "section")},
                                 "stage": "중복", "reason": "최근 게재분과 같은 사안: " + rep_ids[r["id"]][:60]})
        records = [r for r in records if r["id"] not in rep_ids]
        stats["repeat_dropped"] = len(rep_ids)
        same_ids = drop_same_day(records)
        for r in records:
            if r["id"] in same_ids:
                excluded.append({**{k: r[k] for k in ("title", "url", "press", "pub_date", "section")},
                                 "stage": "중복", "reason": "같은 날 같은 사안: " + same_ids[r["id"]][:60]})
        records = [r for r in records if r["id"] not in same_ids]
        stats["same_day_dropped"] = len(same_ids)
        log.info("같은 날 중복(코드 점검): %d건 제외", len(same_ids))
        log.info("최근 게재분 중복(코드 점검): %d건 제외", len(rep_ids))
    records, cut = apply_caps(records, cfg["section_caps"])
    excluded += [{**{k: r[k] for k in ("title", "url", "press", "pub_date", "section")},
                  "stage": "2차", "reason": "섹션 한도 초과"} for r in cut]

    # 저장 (같은 날 재실행이면 합침) + 게재 상한(묶인 일수에 따라 30~70)
    day = until.strftime("%Y-%m-%d")
    path = DAYS_DIR / f"{day}.json"
    prev = read_json(path, {"items": [], "excluded": []})
    have = {r["url"] for r in prev["items"]}
    new = [r for r in records if r["url"] not in have]
    # 중요도 기준 미달은 싣지 않음(상한을 억지로 채우지 않음). 꼭 실어야 할 기사는 예외
    min_imp = cfg.get("min_importance", 3)
    low = [r for r in new if r["importance"] < min_imp and not r.get("must_include")]
    excluded += [{**{k: r[k] for k in ("title", "url", "press", "pub_date", "section")},
                  "stage": "선별", "reason": f"중요도 {r['importance']} (기준 {min_imp} 미만)"} for r in low]
    low_urls = {r["url"] for r in low}
    new = [r for r in new if r["url"] not in low_urls]
    slots = max(0, cap - len(prev["items"]))
    new.sort(key=lambda r: (-r["importance"], 0 if r.get("must_include") else 1,
                            0 if r["section"] == "own" else 1, r["pub_date"]))
    take, rest = new[:slots], new[slots:]
    # 상한을 넘겨도 '꼭 실어야 할' 기사는 추가(최대 must_include_overflow건)
    already_over = sum(1 for r in prev["items"] if "상한 초과 게재" in r.get("tags", []))
    room = max(0, cfg.get("must_include_overflow", 5) - already_over)
    extra = [r for r in rest if r.get("must_include") or r["importance"] >= 5][:room]
    for r in extra:
        r["tags"].append("상한 초과 게재")
    extra_urls = {r["url"] for r in extra}
    excluded += [{**{k: r[k] for k in ("title", "url", "press", "pub_date", "section")},
                  "stage": "선별", "reason": f"게재 상한 {cap}건 (중요도 {r['importance']})"}
                 for r in rest if r["url"] not in extra_urls]
    merged = prev["items"] + take + extra
    write_json(path, {
        "date": day,
        "generated_at": now.isoformat(timespec="minutes"),
        "window": {"from": since.isoformat(timespec="minutes"), "to": until.isoformat(timespec="minutes")},
        "cap": cap,
        "stats": {**stats, "kept": len(merged)},
        "ai": {k: v for k, v in llm.STATS.items()},
        "items": sort_records(merged),
        "excluded": (prev.get("excluded", []) + excluded)[-400:],
    })

    # 상태 갱신
    seen = state["seen"]
    stamp = now.strftime("%Y-%m-%d")
    for it in items:
        seen[it["url"]] = stamp
        for u in it.get("cluster_urls", []):
            seen[u] = stamp
    cutoff = (now - timedelta(days=cfg["seen_retention_days"])).strftime("%Y-%m-%d")
    state["seen"] = {u: d for u, d in seen.items() if d >= cutoff}
    state["last_run"] = now.isoformat(timespec="seconds")
    if not args.hours:
        state["last_window_end"] = until.isoformat(timespec="minutes")
    write_json(STATE_FILE, state)
    update_index()
    log.info("완료: 오늘 게재 누적 %d건, 제외 기록 %d건 → %s", len(merged), len(excluded), path.name)


if __name__ == "__main__":
    main()
