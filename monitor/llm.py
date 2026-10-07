"""AI 판정·요약. 기본은 Gemini API 무료 등급, 설정에서 Claude(유료)로 바꿀 수 있음.

호출 수를 최소화하려고 모두 '묶음' 단위로 보낸다.
- 1차 선별: 기사 수십 건을 한 번에 (제목·요약문)
- 2차 판정·요약: 본문 여러 건을 한 번에
- 중복 점검: 하루 1번
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

import requests

from .common import load_text, settings

log = logging.getLogger(__name__)

SECTION_ENUM = ["own", "fandom_music", "ent_content", "ai_tech"]

# 섹션 안 정렬 순서 (위클리 배치 순서 기준)
SUBSECTIONS = {
    "own": ["own"],
    "fandom_music": ["fan_platform", "music_platform", "global_music", "music_ai_rights"],
    "ent_content": ["fandom_commerce", "ent_biz", "ent_market", "live_ticket",
                    "virtual_ai", "video_platform"],
    "ai_tech": ["ai_tech"],
}
SUB_DESC = """subsection 값:
- own: 자사
- fan_platform: 팬덤 플랫폼·팬덤 비즈니스 일반
- music_platform: 국내 음악 플랫폼·음원 시장
- global_music: 해외 음악 플랫폼·해외 음악산업(영문 기사 포함)
- music_ai_rights: 음악 AI 서비스, 플랫폼 관련 저작권 분쟁
- fandom_commerce: 굿즈·MD·팝업·캐릭터 IP·팬덤 커머스·카페24 등
- ent_biz: 엔터사·유통사 실적·사업 전략·투자·신사업
- ent_market: 엔터 산업 분석·엔터주 구조 분석·정책/상표/제도
- live_ticket: 공연·콘서트·티켓·암표
- virtual_ai: 버추얼·AI 아이돌, AI 창작물 저작권
- video_platform: 유튜브·OTT·숏폼·FAST
- ai_tech: AI & Tech"""

ALL_SUBS = [s for v in SUBSECTIONS.values() for s in v]


class QuotaExceeded(RuntimeError):
    """무료 한도 소진 — 남은 기사는 AI 없이 처리."""


class ServerBusy(RuntimeError):
    """모델 서버 과부하(5xx)가 계속됨 — 다른 모델로 넘겨서 시도."""


# 실행 진단용 집계(결과 파일에 저장)
STATS = {"calls": 0, "ok": 0, "parse_fail": 0, "rate_wait": 0, "quota_day": 0, "errors": []}


# ── 공급자 공통 호출 ─────────────────────────────
_lock = threading.Lock()
_last_call = [0.0]


def _pace(interval: float) -> None:
    """분당 호출 제한을 넘지 않도록 호출 간격 유지."""
    with _lock:
        wait = _last_call[0] + interval - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _criteria() -> str:
    return ("당신은 PR 에이전시의 뉴스 모니터링 담당자입니다. 아래 기준을 엄격히 따릅니다.\n\n"
            + load_text("criteria.md"))


EXHAUSTED: set = set()   # 오늘 하루 한도가 바닥난 모델


def call_json(task: str, guide: str, user: str, schema: dict, max_tokens: int) -> dict:
    cfg = settings()
    provider = cfg.get("ai_provider", "gemini")
    if provider != "gemini":
        _pace(cfg.get("call_interval_sec", 5))
        models = cfg["models"][provider]
        return _claude(models.get(task, models["review"]), _criteria(), guide, user, schema, max_tokens)
    # 지정 모델 → 한도가 바닥나면 다른 무료 모델로 넘어가며 시도
    gm = cfg["models"]["gemini"]
    order = [gm.get(task, gm["review"])] + cfg.get("gemini_fallback_models",
                                                      ["gemini-flash-lite-latest", "gemini-flash-latest"])
    tried, busy = [], False
    for model in order:
        if model in EXHAUSTED or model in tried:
            continue
        tried.append(model)
        _pace(cfg.get("call_interval_sec", 5))
        try:
            return _gemini(model, _criteria() + "\n\n" + guide, user, schema, max_tokens)
        except QuotaExceeded as e:
            if "시간 예산" in str(e):
                raise
            log.warning("모델 %s 오늘 한도 소진 → 다른 모델로 전환", model)
            EXHAUSTED.add(model)
            STATS.setdefault("exhausted", []).append(model)
        except ServerBusy:
            log.warning("모델 %s 서버 과부하 지속 → 다른 모델로 시도", model)
            busy = True
    if busy:
        raise RuntimeError("모든 모델 서버 과부하(5xx) — 이 묶음은 나중에 다시 시도")
    raise QuotaExceeded("모든 무료 모델 한도 소진")


def _to_gemini_schema(s: dict) -> dict:
    """JSON Schema → Gemini responseSchema(OpenAPI 부분집합)."""
    out = {}
    for k, v in s.items():
        if k == "type":
            out["type"] = v.upper()
        elif k == "properties":
            out["properties"] = {pk: _to_gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out["items"] = _to_gemini_schema(v)
        elif k in ("enum", "required", "description"):
            out[k] = v
    return out


DEADLINE = [None]          # run_daily가 시작할 때 설정 — 이 시각이 지나면 AI 호출 중단
_THINKING = {"mode": "low"}  # 'low' → 거부되면 'off'(설정 안 보냄)


def _retry_delay(text: str, default: float) -> float:
    m = re.search(r'"retryDelay":\s*"(\d+)(?:\.\d+)?s"', text)
    return min(float(m.group(1)) + 1, 90) if m else default


def _gemini(model: str, system: str, user: str, schema: dict, max_tokens: int) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY 환경변수가 없습니다.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen = {
        "responseMimeType": "application/json",
        "responseSchema": _to_gemini_schema(schema),
        "maxOutputTokens": min(max(max_tokens * 3, 8192), 32768),
        "temperature": 0.2,
    }
    server_err = 0
    for attempt in range(4):
        if DEADLINE[0] and time.time() > DEADLINE[0]:
            raise QuotaExceeded("AI 처리 시간 예산 초과")
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": dict(gen)}
        if _THINKING["mode"] == "low":
            # 판정·요약에는 긴 '생각'이 필요 없어 속도를 위해 최소화(지원 안 하는 모델이면 자동 해제)
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
        STATS["calls"] += 1
        t0 = time.time()
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=150)
        except requests.RequestException as e:
            log.warning("Gemini 연결 오류/시간 초과, 재시도 %d: %s", attempt + 1, e)
            STATS["errors"].append(f"연결 오류: {str(e)[:80]}")
            time.sleep(5)
            continue
        STATS.setdefault("seconds", 0)
        STATS["seconds"] += round(time.time() - t0)
        if r.status_code == 400 and "thinking" in r.text.lower() and _THINKING["mode"] == "low":
            log.warning("thinkingConfig 미지원 → 해제 후 재시도")
            _THINKING["mode"] = "off"
            continue
        if r.status_code == 429:
            msg = r.text
            if "PerDay" in msg or "per day" in msg.lower():
                STATS["quota_day"] += 1
                STATS["errors"].append("429 하루 한도: " + msg[:150])
                raise QuotaExceeded("Gemini 하루 무료 한도 소진")
            STATS["rate_wait"] += 1
            wait = _retry_delay(msg, 20)
            log.warning("Gemini 분당 한도, %ds 대기", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            STATS["errors"].append(f"{r.status_code} 서버 오류")
            server_err += 1
            if server_err >= 3:          # 같은 모델이 계속 과부하면 기다리지 말고 다른 모델로
                raise ServerBusy(f"{model} {r.status_code}")
            time.sleep(10 * 2 ** (server_err - 1))   # 10초 → 20초
            continue
        if r.status_code == 404 and model != "gemini-flash-latest":
            log.warning("모델 %s 없음 → gemini-flash-latest로 대체", model)
            STATS["errors"].append(f"모델 없음: {model}")
            return _gemini("gemini-flash-latest", system, user, schema, max_tokens)
        if r.status_code != 200:
            STATS["errors"].append(f"{r.status_code}: {r.text[:200]}")
            raise RuntimeError(f"Gemini 오류 {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            out = json.loads(_strip_fence(text))
            STATS["ok"] += 1
            return out
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            STATS["parse_fail"] += 1
            fr = (data.get("candidates") or [{}])[0].get("finishReason", "?")
            log.warning("Gemini 응답 해석 실패(finishReason=%s), 재시도 %d: %s", fr, attempt + 1, e)
            if len(STATS["errors"]) < 30:
                STATS["errors"].append(f"해석 실패 finishReason={fr}")
            if attempt >= 1:   # 두 번 연속 실패면 묶음을 쪼개도록 넘김
                break
    if STATS["rate_wait"] >= 6 and STATS["ok"] == 0:
        raise QuotaExceeded("Gemini 분당 한도가 풀리지 않음")
    raise RuntimeError("Gemini 응답을 받지 못했습니다")


def _strip_fence(t: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip())


def _claude(model: str, criteria: str, guide: str, user: str, schema: dict, max_tokens: int) -> dict:
    import anthropic  # 유료 옵션을 쓸 때만 필요
    client = anthropic.Anthropic(max_retries=4)
    tool = {"name": "result", "description": "결과", "input_schema": schema}
    system = [{"type": "text", "text": criteria, "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": guide}]
    for attempt in range(3):
        try:
            msg = client.messages.create(
                model=model, max_tokens=max_tokens, system=system, tools=[tool],
                tool_choice={"type": "tool", "name": "result"},
                messages=[{"role": "user", "content": user}])
            for b in msg.content:
                if b.type == "tool_use":
                    return b.input
        except anthropic.APIError as e:
            log.warning("Claude 오류, 재시도 %d: %s", attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("Claude 호출 실패")


# ── 1차: 제목·요약문 일괄 선별 ───────────────────
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["include", "maybe", "exclude"]},
                    "section": {"type": "string", "enum": SECTION_ENUM},
                    "importance": {"type": "integer", "description": "1~5"},
                    "reason": {"type": "string", "description": "20자 이내"},
                },
                "required": ["id", "decision", "section", "importance", "reason"],
            },
        }
    },
    "required": ["results"],
}

TRIAGE_GUIDE = """[작업] 1차 선별. 제목과 요약문(본문 앞부분)만 보고 판단한다.
- include: 기준상 포함이 확실
- maybe: 본문을 봐야 판단 가능 (특히 자사 키워드로 검색됐는데 요약문에 자사가 안 보이는 경우, 트렌드 기획일 가능성이 있는 경우)
- exclude: 기준상 제외가 확실 (단순 언급, 단순 주가, 연예·아티스트 단신, 무관한 동음이의어 등)
importance: 5=비마프·드림어스 사업에 직접 영향, 4=업계 흐름을 보여주는 기획·분석, 3=참고할 만한 업계 소식, 1~2=일반 소식. 하루치 30건 안팎만 실리므로 엄격하게.
reason은 판단 근거를 20자 이내로. 입력된 모든 id에 대해 빠짐없이 답한다."""


def triage(items: list[dict]) -> dict[str, dict]:
    lines = [json.dumps({
        "id": it["id"], "title": it["title"], "snippet": it["description"][:200],
        "lang": it.get("lang", "ko"), "검색어": ",".join(it.get("queries", []))[:50],
        "섹션힌트": ",".join(it.get("section_hints", [])), "동일보도수": it.get("cluster_size", 1),
    }, ensure_ascii=False) for it in items]
    out = call_json("triage", TRIAGE_GUIDE, "다음 기사들을 1차 선별하세요.\n" + "\n".join(lines),
                    TRIAGE_SCHEMA, max_tokens=70 * len(items) + 800)
    return {r["id"]: r for r in out.get("results", []) if "id" in r}


# ── 2차: 본문 판정 + 요약 (여러 건 묶음) ───────────
REVIEW_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "keep": {"type": "boolean"},
        "reason": {"type": "string", "description": "판정 근거 40자 이내"},
        "section": {"type": "string", "enum": SECTION_ENUM},
        "subsection": {"type": "string", "enum": ALL_SUBS},
        "importance": {"type": "integer", "description": "1~5"},
        "kind": {"type": "string", "enum": ["news", "trend", "interview", "column", "disclosure"]},
        "press": {"type": "string", "description": "언론사명(한글 공식 표기, 영문 매체는 영문)"},
        "press_release_guess": {"type": "boolean", "description": "자사가 낸 보도자료 기사로 보이면 true"},
        "must_include": {"type": "boolean", "description": "하루 상한을 넘겨서라도 반드시 실어야 할 기사면 true (드물게)"},
        "summary": {"type": "string", "description": "keep=true일 때만. 일반 440~500자, 기획·칼럼·인터뷰 550~650자. 기사에 없는 평가·전망 금지. 기준 8번 준수"},
    },
    "required": ["id", "keep", "reason", "section", "subsection", "importance", "kind", "press"],
}
REVIEW_SCHEMA = {"type": "object", "properties": {"results": {"type": "array", "items": REVIEW_ITEM}},
                 "required": ["results"]}

REVIEW_GUIDE = """[작업] 2차 판정. 기사마다 본문을 읽고 클리핑 포함 여부(keep)를 최종 결정하고, 포함이면 요약을 쓴다.
- '단순 언급'인지 반드시 본문에서 확인한다. 자사는 실질 서술이 있으면 포함하고 해당 문장을 요약에 **굵게** 넣는다.
- 엔터사 기사는 기업 이슈(실적·사업·투자·지배구조·플랫폼 전략)일 때만 포함한다.
- 본문을 가져오지 못해 요약문만 있는 경우, 확인 가능한 범위에서만 요약하고 지어내지 않는다.
- 요약은 줄바꿈 없이 한 단락. 입력된 모든 id에 대해 답한다.
- 요약 분량은 기준 8번을 반드시 지킨다: 일반 기사 440~500자(약 10줄), 기획·분석·칼럼·인터뷰 550~650자(약 12~14줄). 본문의 배경·수치·발언·향후 계획으로 채우고, 기사에 없는 평가·전망·의미 부여 문장("~할 전망이다", "~로 평가할 수 있다", "~시사점을 제공한다" 등)으로 채우지 않는다.
- 요약의 모든 문장은 기사 본문에 근거가 있어야 한다. 기준 문서의 클라이언트 설명(드림어스 자회사·투자사, 비스테이지 고객 등)은 판단용 배경지식일 뿐이니 요약에 옮겨 쓰지 않는다. 회사 간 관계(자회사·계열사·투자·인수)는 본문에 그 표현이 있을 때만 쓴다. 자사 문장도 마찬가지다. 시스템이 요약을 본문과 대조해 근거 없는 문장은 자동으로 지운다.
- 본문에 이 분량을 채울 사실이 없는 기사(내용이 빈약한 단신·홍보성 짧은 글)는 keep=false, reason="내용 부족"으로 둔다. 단 자사 기사는 짧아도 포함한다.
- 사이트에는 하루치 기준 30건만 실리고(월요일·연휴 뒤는 더 많음), 중요도 3 미만은 싣지 않는다. importance를 엄격하게 매긴다:
  5 = 비마프·드림어스 사업에 직접 영향(자사 실질 기사, 경쟁 팬덤·음악 플랫폼의 주요 발표, 엔터사 실적·핵심 사업 전략)
  4 = 팬덤 플랫폼·음악 플랫폼·엔터 산업의 흐름을 보여주는 기획·분석 기사, 주요 플레이어의 의미 있는 사업 발표
  3 = 팬덤·음악·엔터 산업과 직접 관련된 참고 소식
  2 이하 = 관련성 기준을 통과하지 못하거나 간접적인 기사 → keep=false
  대부분의 기사는 3이다. 4·5는 정말 그럴 때만 준다. 관련성 기준이 애매하면 keep=false.
- must_include=true는 아주 드물게만: 자사 핵심 기사(신규 사업·실적·대형 제휴), 업계 판도를 바꾸는 발표(경쟁 플랫폼 출시·종료·인수합병, 주요 엔터사 실적·지배구조 변화, 음원 유통·저작권 제도 변경). 하루 0~2건이 정상.
""" + SUB_DESC


def review_batch(items: list[dict], max_chars: int, force: bool = False, note: str = "",
                 task: str = "review") -> dict[str, dict]:
    docs = []
    for it in items:
        docs.append({
            "id": it["id"], "제목": it["title"], "언론사(추정)": it.get("press") or "",
            "발행": it["pub_date"], "언어": it.get("lang", "ko"), "검색어": it.get("queries", []),
            "동일보도수": it.get("cluster_size", 1), "1차판단": it.get("triage", {}).get("reason", ""),
            "본문확보": it.get("body_ok", False),
            "본문": (it.get("body") or it.get("description") or "")[:max_chars],
        })
    user = json.dumps(docs, ensure_ascii=False)
    if force:
        user += "\n※ 담당자가 직접 추가한 기사다. keep=true로 두고 요약을 작성한다."
    if note:
        user += "\n※ " + note
    out = call_json(task, REVIEW_GUIDE, user, REVIEW_SCHEMA, max_tokens=1400 * len(items) + 500)
    return {r["id"]: r for r in out.get("results", []) if "id" in r}


# ── 3차: 전날·당일 게재분과 같은 사안인지 한 번에 점검 ──────
DEDUPE_SCHEMA = {
    "type": "object",
    "properties": {
        "drop": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "빼야 할 오늘 후보의 id"},
                    "same_as": {"type": "string", "description": "겹치는 기사 제목"},
                },
                "required": ["id", "same_as"],
            },
        }
    },
    "required": ["drop"],
}

DEDUPE_GUIDE = """[작업] 중복 점검.
'이미 게재'는 최근 며칠 동안 사이트에 올라간 기사다. '오늘 후보' 중
1) 이미 게재된 기사와 같은 발표·같은 사건을 다루는 기사(매체·제목이 달라도 같은 사안이면 중복)
2) 오늘 후보끼리 같은 사안인 기사(중요도·정보량이 낮은 쪽을 뺀다)
를 drop에 넣는다. 새로운 사실(후속 수치, 공식 입장, 반론)이 더해졌거나 기획·분석으로 확장된 기사는 중복이 아니다.
확실한 경우만 넣고, 애매하면 남긴다. 해당이 없으면 빈 배열."""


def dedupe(today: list[dict], recent: list[dict]) -> list[dict]:
    if not today:
        return []
    fmt = lambda r: {"title": r["title"], "press": r.get("press", ""), "lead": (r.get("summary") or "")[:160]}
    user = json.dumps({
        "이미 게재": [fmt(r) for r in recent],
        "오늘 후보": [{"id": r["id"], **fmt(r), "importance": r.get("importance", 3)} for r in today],
    }, ensure_ascii=False)
    out = call_json("review", DEDUPE_GUIDE, user, DEDUPE_SCHEMA, max_tokens=2000)
    ids = {r["id"] for r in today}
    return [d for d in out.get("drop", []) if d.get("id") in ids]
