"""규칙 기반 1차 필터, 중복 기사 묶기, 시리즈 기획 감지."""
from __future__ import annotations

import collections
import itertools
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


# ── 같은 사안 판별(제목·요약의 드문 단어 공유) ──────
# 같은 사건을 매체마다 제목을 달리 쓰면 cluster()로 안 묶인다(예: 암표 국감 기사 15건).
# 후보 안에서 드물게 나오는 단어를 몇 개 이상 함께 쓰면 같은 사안으로 본다.
_TOPIC_STOP = set("""기자 지난 올해 지난해 이번 위해 대한 관련 통해 따르면 밝혔다 있다 했다 한다 있으며 오는 최근
가장 사진 뉴스 단독 종합 공식 국내 글로벌 한국 서울 신규 출시 개최 진행 예정 전년 대비 기록 확대 강화 나선다
공개 결정 계획 이후 현재 통한 위한 대해 함께 중심 가운데 이어 특히 또한 다만 한편 지난달 이달 내년""".split())
_JOSA = re.compile(r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만|까지|부터)$")


def topic_tokens(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[0-9A-Za-z가-힣]{2,}", (text or "").replace("**", "")):
        w = _JOSA.sub("", w).lower()
        if len(w) >= 2 and w not in _TOPIC_STOP:
            out.add(w)
    return out


def topic_links(texts: list[str], k: int, max_df_ratio: float = 0.05) -> list[set[int]]:
    """texts[i]와 같은 사안으로 보이는 다른 인덱스 집합(드문 단어를 k개 이상 공유).
    여러 건을 사슬처럼 엮지 않고 1:1 쌍만 본다(엉뚱한 기사끼리 묶이는 것 방지)."""
    toks = [topic_tokens(t) for t in texts]
    df = collections.Counter(x for s in toks for x in s)
    lim = max(3, int(len(texts) * max_df_ratio))
    links: list[set[int]] = [set() for _ in texts]
    for i, j in itertools.combinations(range(len(texts)), 2):
        if len({x for x in toks[i] & toks[j] if df[x] <= lim}) >= k:
            links[i].add(j)
            links[j].add(i)
    return links
