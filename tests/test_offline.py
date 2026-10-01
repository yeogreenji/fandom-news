"""네트워크·API 없이 파이프라인 흐름 점검 (가짜 응답 사용)."""
import shutil, tempfile, json
from datetime import timedelta
from pathlib import Path
import monitor.common as C

tmp = Path(tempfile.mkdtemp())
C.DAYS_DIR = tmp / "days"; C.DATA_DIR = tmp; C.STATE_FILE = tmp / "state.json"
import monitor.run_daily as R
R.DAYS_DIR = C.DAYS_DIR; R.STATE_FILE = C.STATE_FILE
import monitor.sources as S, monitor.llm as L

now = C.now_kst()
def fake_search(q, since, limit):
    base = [
        {"title": "카카오엔터, 멜론·베리즈 계정 연동…K팝 팬 활동 한곳서", "description": "카카오엔터테인먼트는 29일 멜론과 베리즈의 계정 연동을 시작한다.", "url": "https://www.ajunews.com/a1", "naver_url": "https://n.news.naver.com/a1", "pub_date": now.isoformat(), "lang": "ko"},
        {"title": "카카오엔터 멜론·베리즈 계정 연동 시작", "description": "카카오엔터테인먼트는 29일 멜론과 베리즈의 계정 연동을 시작한다고 밝혔다.", "url": "https://www.newsis.com/a2", "naver_url": None, "pub_date": now.isoformat(), "lang": "ko"},
        {"title": "[포토] 위버스 콘서트 현장", "description": "위버스", "url": "https://x.com/p", "naver_url": None, "pub_date": now.isoformat(), "lang": "ko"},
        {"title": "에픽하이, 데뷔 후 첫 KSPO돔 입성", "description": "그룹 에픽하이가 내년 1월 단독 콘서트로...", "url": "https://www.topstarnews.net/e1", "naver_url": None, "pub_date": now.isoformat(), "lang": "ko"},
        {"title": "[K팝과 AI 혁명 ③] 제작비 1000분의 1 수준", "description": "AI 음악 제작...", "url": "https://isplus.com/s3", "naver_url": None, "pub_date": now.isoformat(), "lang": "ko"},
    ]
    return [dict(b) for b in base]
S.naver_search = fake_search
S.rss_fetch = lambda url, since: []
def fake_triage(items):
    return {it["id"]: {"id": it["id"], "decision": "maybe", "section": "fandom_music", "importance": 3, "reason": "테스트"} for it in items}
def fake_review_one(item):
    own = "에픽하이" in item["title"]
    return {"keep": True, "reason": "ok", "section": "own" if own else ("ent_content" if "AI" in item["title"] else "fandom_music"),
            "subsection": "own" if own else ("virtual_ai" if "AI" in item["title"] else "fan_platform"), "importance": 4, "kind": "news",
            "press": item.get("press") or "테스트", "summary": "요약 문장. **비스테이지 문장.**"}
L.triage = fake_triage
L.review_batch = lambda items, mx, force=False: {i["id"]: {**fake_review_one(i), "id": i["id"]} for i in items}
L.dedupe = lambda today, recent: [{'id': r['id'], 'same_as': 'x'} for r in today if '에픽하이' in r['title'] and recent]
R.fetch_article = lambda it: it.update(body="본문"*200, body_ok=True, press=it.get("press","")) or it
R.main(["--hours", "24"])
day = json.load(open(next(C.DAYS_DIR.glob("*.json"))))
print(json.dumps(day["stats"], ensure_ascii=False))
for r in day["items"]: print(r["section"], r["subsection"], r["press"], r["title"], r["tags"], r["series"])
assert len(day["items"]) == 3, len(day["items"])   # 포토 제거, 중복 1건 묶임
# 재실행: seen 처리로 신규 0
R.main(["--hours", "24"])
day2 = json.load(open(next(C.DAYS_DIR.glob("*.json"))))
assert len(day2["items"]) == 3
print("OK")
shutil.rmtree(tmp)
