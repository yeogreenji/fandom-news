"""규칙 기반 1차 필터, 중복 기사 묶기, 시리즈 기획 감지."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .press import press_from_domain, PRESS_PRIORITY

# 제목만 보고 바로 버리는 유형 (AI 비용 절약용)
EXCLUDE_TITLE = re.compile(
    r"(\[\s*(포토|사진|화보|영상|PHOTO|HD포토|SS포토|MD포토|포토뉴스|인사|부고|게시판|알림|운세|날씨|"
    r"오늘의\s*운세|퀴즈|정답|오늘의 날씨|카드뉴스)\s*\]"
    r"|특징주|\[?\s*장중\s*수급|상한가|하한가|급등주|급락주|오늘의\s*운세|퀴즈\s*정답|\bOX퀴즈)",
    re.IGNORECASE,
)

SERIES_RE = re.compile(r"\[([^\]\[]{2,30}?)\s*([①-⑳]|\d{1,2}(?:편|회)?|\(\d{1,2}\))\s*\]")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def rule_excluded(title: str) -> bool:
    return bool(EXCLUDE_TITLE.search(title or ""))


def snippet_has(item: dict, words: list[str]) -> bool:
    hay = (item["title"] + " " + item["description"]).replace(" ", "").lower()
    return any(w.replace(" ", "").lower() in hay for w in words)


def query_terms(q: str) -> list[str]:
    """'디어유 버블' → 두 단어 모두 있어야 한다는 의미로 리스트 반환."""
    return [t for t in q.split() if t]


def snippet_has_all(item: dict, terms: list[str]) -> bool:
    hay = (item["title"] + " " + item["description"]).replace(" ", "").lower()
    return all(t.lower() in hay for t in terms)


# ── 중복 보도 묶기 ─────────────────────────────
_norm_re = re.compile(r"[\[\(【<〈「『][^\]\)】>〉」』]*[\]\)】>〉」』]|[^0-9A-Za-z가-힣]")


def _norm_title(t: str) -> str:
    return _norm_re.sub("", t or "").lower()


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _sim(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    except Exception:
        return ""


def _rank(item: dict) -> tuple:
    press = press_from_domain(item["url"]) or ""
    pri = PRESS_PRIORITY.index(press) if press in PRESS_PRIORITY else len(PRESS_PRIORITY)
    has_naver = 0 if item.get("naver_url") else 1
    return (pri, has_naver, item["pub_date"])


def cluster(items: list[dict], threshold: float = 0.45) -> list[dict]:
    """비슷한 제목(같은 보도자료 재게재 등)을 묶고 대표 기사 1건만 남긴다.
    대표 기사에 cluster_size, cluster_urls 기록."""
    groups: list[list[dict]] = []
    sigs: list[set] = []
    desc_keys: list[str] = []
    for it in items:
        sig = _bigrams(_norm_title(it["title"]))
        dkey = _norm_title(it["description"])[:40]
        placed = False
        for gi, gsig in enumerate(sigs):
            if _sim(sig, gsig) >= threshold or (len(dkey) >= 30 and dkey == desc_keys[gi]):
                groups[gi].append(it)
                placed = True
                break
        if not placed:
            groups.append([it])
            sigs.append(sig)
            desc_keys.append(dkey)
    reps = []
    for g in groups:
        g.sort(key=_rank)
        rep = dict(g[0])
        rep["cluster_size"] = len(g)
        rep["cluster_urls"] = [x["url"] for x in g[1:]]
        rep["queries"] = sorted({q for x in g for q in x.get("queries", [])})
        rep["section_hints"] = sorted({h for x in g for h in x.get("section_hints", [])})
        reps.append(rep)
    return reps


def detect_series(title: str) -> dict | None:
    m = SERIES_RE.search(title or "")
    if not m:
        return None
    name, no = m.group(1).strip(), m.group(2)
    if no in CIRCLED:
        n = CIRCLED.index(no) + 1
    else:
        n = int(re.sub(r"\D", "", no) or 0)
    return {"name": name, "no": n}
