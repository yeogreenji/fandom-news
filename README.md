# Fandom & Music IP Business Trend News — 데일리 모니터링

매일 아침 네이버 뉴스와 해외 음악산업 매체(Music Business Worldwide, Billboard)에서 기사를 모으고, AI가 단순 언급 기사를 걸러낸 뒤 위클리 형식(제목 링크 · [매체명] – 날짜 · 요약)으로 정리해 웹사이트에 올립니다.

```
월~금 09:30 자동 실행 (GitHub Actions) — 직전 영업일 09:30 ~ 오늘 09:30 기사. 월요일·연휴 다음 날은 쉬는 날 기사까지 묶음
 ├ 1. 네이버 뉴스 검색 API: config/queries.yaml 검색어 약 120개, 직전 실행 이후 기사
 ├ 2. 규칙 필터: 포토·특징주·운세 등 제거, 제목/요약문에 키워드 없는 기사 제거(자사 제외)
 ├ 3. 중복 묶기: 같은 보도자료 재게재는 대표 1건만 (통신사·주요지 우선)
 ├ 4. AI 1차 선별 (Gemini 무료): 제목·요약문 60건씩 묶어 포함/보류/제외
 ├ 5. 본문 수집: 네이버 뉴스 페이지 → 언론사 원문 순
 ├ 6. AI 2차 판정 + 요약 (Gemini 무료): 본문 6건씩 묶어 단순 언급 여부 확정, 섹션 분류, 4~7문장 요약
 ├ 7. 게재 상한: 하루치 30 / 이틀치 40 / 사흘치(월) 50 / 그 이상 60~70건. 중요도 3 미만은 안 실음(적은 날은 적게). 꼭 실어야 할 기사는 최대 5건 초과 허용
 ├ 8. 중복 점검: 최근 3일 게재분·오늘 후보끼리 같은 사안이면 제외 (제목이 달라도 같은 발표·사건이면 중복)
 └ 9. docs/data/days/날짜.json 저장 → 웹사이트 자동 갱신
```

## 처음 설정 (한 번만, 약 30분)

### 1) 네이버 검색 API 키 발급 — NAVER API HUB
네이버 개발자센터의 검색 API 신규 발급은 2026년 7월 31일 종료됐고, 네이버 클라우드의 **NAVER API HUB**로 옮겨졌습니다.
1. https://www.ncloud.com 회원가입 (네이버 아이디 연동 가능). 가입 승인에 **결제 카드 등록이 필요**합니다.
2. 콘솔 → Services → Application Services → **NAVER API HUB** → 이용 신청 → 약관 동의
3. **Application 등록** → 이름 입력 → API에서 **NAVER 검색** 선택 → 등록
4. 인증 정보에 나오는 **Client ID / Client Secret** 복사
5. (권장) 콘솔에서 이용 한도·임계치 알림 설정

> 2026년 10월 현재 무료(검색 API 월 775,000건, 이 시스템은 월 1만 건 안팎). 유료 전환 시 사전 공지 예정.
> 2026년 7월 이전에 개발자센터에서 받은 키가 있으면 그대로 써도 됩니다(2027년 6월 30일까지): 저장소 Settings → Secrets and variables → Actions → **Variables**에 `NAVER_API_MODE` = `legacy` 추가.

### 2) Gemini API 키 발급 (무료)
1. https://aistudio.google.com 에 구글 계정으로 로그인
2. 왼쪽 아래 **Get API key → Create API key** → 키 복사
3. **결제 정보(Billing)는 연결하지 마세요.** 결제를 연결하지 않으면 무료 등급으로만 동작하고, 한도를 넘으면 요금이 나가는 대신 그날 호출이 멈춥니다.

> 무료 등급 한도(하루·분당 호출 수)는 구글이 수시로 조정합니다. AI Studio의 Rate limit 화면에서 현재 한도를 볼 수 있습니다. 이 시스템은 기사를 묶어 보내고 게재 건수를 제한해서 하루 15~30회(월요일은 조금 더) 정도만 호출합니다.

### 3) GitHub 저장소 만들기
1. GitHub → 우측 상단 **+ → New repository**
   - 이름: 예) `fandom-news` · **Public** 선택 (무료 계정은 Public 저장소만 웹사이트 공개 가능)
2. 만든 저장소에서 **Add file → Upload files** → 압축을 푼 폴더 안의 내용 전체를 끌어다 놓기
   - `.github` 폴더가 꼭 같이 올라가야 합니다 (숨김 폴더라 탐색기에서 안 보이면 "숨긴 항목 표시"를 켜세요)
3. **Commit changes**

### 4) 비밀 키 등록
저장소 **Settings → Secrets and variables → Actions → New repository secret** 에서 3개 등록:

| Name | Value |
|---|---|
| `NAVER_CLIENT_ID` | 네이버 Client ID |
| `NAVER_CLIENT_SECRET` | 네이버 Client Secret |
| `GEMINI_API_KEY` | Gemini API 키 |

키는 저장소가 Public이어도 외부에 보이지 않습니다.

### 5) 권한·웹사이트 설정
1. **Settings → Actions → General → Workflow permissions** → **Read and write permissions** 선택 → Save
2. **Settings → Pages → Build and deployment → Source** → **GitHub Actions** 선택

### 6) 첫 실행
1. 상단 **Actions** 탭 → 왼쪽 **Daily monitor** → **Run workflow** → hours 칸에 `24` → Run
   (처음 실행할 때 주말·연휴가 끼어 있으면 hours에 `72` 등 원하는 시간을 넣으세요)
2. 10~20분 뒤 완료되면 사이트 주소: `https://<GitHub아이디>.github.io/<저장소이름>/`

이후로는 월~금 09:30에 자동 실행됩니다. 한국 공휴일·대체공휴일·선거일에는 자동으로 쉬고, 다음 영업일에 쉬는 날 기사까지 묶어서 올립니다. 회사 휴무일은 `config/settings.yaml`의 `extra_holidays`에 날짜를 넣으면 됩니다. GitHub 서버 사정으로 실행이 10~30분 늦어지는 날이 있지만, 수집 구간은 09:30에 고정돼 있어 누락·중복은 없습니다.

## 사이트 쓰는 법

- **일간 / 기간**: 기간을 고르면 여러 날을 한 화면에 모아 봅니다 (위클리 작성용).
- **체크 → 「메일 형식으로 복사」**: 체크한 기사만 위클리 양식(섹션 제목, 링크 걸린 제목, [매체] – 날짜, 요약, 굵게 표시)으로 복사됩니다. 아웃룩·지메일 본문에 붙여넣으면 서식이 유지됩니다. 「머리말 포함」을 켜면 모니터링 기간·대상 문구가 위에 붙습니다.
- 같은 시리즈 기획(예: `[K팝과 AI 혁명 ①]`)이 2건 이상이면 맨 아래 **[추가]** 블록에 제목만 모아 보여줍니다.
- 체크 상태는 각자 브라우저에만 저장됩니다. 클라이언트와 같이 봐도 서로 영향 없습니다.
- **점검 모드**: 주소 끝에 `#review`를 붙여 열면(`.../#review`) 「제외된 기사」 탭과 AI 판정 사유가 보입니다. 필터가 잘못 걸렀는지 확인할 때 쓰세요. (클라이언트에게는 `#review` 없는 주소만 공유)

## 운영 중 손볼 때

| 하고 싶은 것 | 방법 |
|---|---|
| 빠진 기사 직접 추가 | Actions → **Manual edit** → Run workflow → action `add`, 기사 링크, 섹션 선택 |
| 잘못 들어간 기사 빼기 | Actions → **Manual edit** → action `remove`, 기사 링크 |
| 검색어 추가·삭제 | `config/queries.yaml` 파일을 GitHub에서 열고 ✏️ 편집 → Commit |
| 포함/제외 기준·요약 문체 수정 | `config/criteria.md` 편집 (AI 프롬프트에 그대로 들어갑니다) |
| AI&Tech 하루 건수 | `config/settings.yaml`의 `section_caps` |
| 수집 기준시각 변경 | `settings.yaml`의 `cutoff_time`과 `.github/workflows/daily.yml`의 `cron`(UTC, 한국시간 −9시간)을 함께 수정 |
| 회사 휴무일 지정 | `settings.yaml`의 `extra_holidays`에 날짜 추가 |
| 상한·중요도 기준 | `settings.yaml`의 `cap_base`, `cap_per_extra_day`, `cap_max`, `min_importance` |

## 비용

**0원으로 운영됩니다.**
- 네이버 검색 API: 결제 수단 등록 없이 쓰는 무료 API, 하루 25,000회 한도 (이 시스템은 하루 수백 회)
- GitHub Actions·Pages: Public 저장소는 무료
- Gemini API: 결제 미연결 무료 등급

무료 AI 한도가 그날 바닥나면 나머지 기사는 AI 없이 처리합니다. 자사 기사와 1차에서 '포함'으로 판정된 기사만 네이버 요약문(1~2문장)으로 올라가고 「AI 미판정」 표시가 붙습니다. 다음 날 한도가 다시 채워집니다.

> 나중에 요약 품질을 더 높이고 싶으면 `config/settings.yaml`의 `ai_provider`를 `claude`로 바꾸고 `ANTHROPIC_API_KEY`를 등록하면 됩니다(유료, 월 4~8만 원 추정).

## 알아둘 점

- 사이트 주소를 아는 사람은 누구나 볼 수 있습니다(검색엔진 노출은 막아 둠). 공개 뉴스 기사만 담기므로 큰 문제는 없지만, 접속 제한이 필요하면 Cloudflare Access 같은 별도 서비스를 붙여야 합니다.
- 저장소가 Public이라 코드·판정 기준·수집 기록도 공개됩니다. 키는 Secrets에 있어 보이지 않습니다.
- 일부 언론사는 본문 수집을 막아 둬서, 그런 기사는 네이버 요약문 기준으로만 판단·요약되고 「본문 미확보」 표시가 붙습니다.
- 무료 등급에서는 보낸 내용이 구글 제품 개선에 쓰일 수 있습니다. 공개된 뉴스 기사만 보내므로 기밀 문제는 없지만, 내부 자료는 이 시스템에 넣지 마세요.
- AI 판정이 100% 정확하지는 않습니다. 처음 1~2주는 `#review` 모드로 제외 사유를 보면서 `criteria.md`를 다듬는 것을 권장합니다.

## 폴더 구조

```
config/        검색어·판정 기준·설정 (운영 중 수정하는 곳)
monitor/       수집·판정 코드
docs/          웹사이트 (index.html, app.js, style.css, data/)
state/         중복 방지용 기록 (사이트에는 안 올라감)
.github/       자동 실행 설정
tests/         오프라인 점검 스크립트
```
