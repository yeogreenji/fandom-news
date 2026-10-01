"""언론사명 매핑. 네이버 기사 페이지에서 못 찾을 때 도메인으로 보완."""
from __future__ import annotations

from urllib.parse import urlparse

DOMAIN_PRESS = {
    "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스", "news1.kr": "뉴스1",
    "edaily.co.kr": "이데일리", "hankyung.com": "한국경제", "mk.co.kr": "매일경제",
    "mt.co.kr": "머니투데이", "chosun.com": "조선일보", "biz.chosun.com": "조선비즈",
    "it.chosun.com": "IT조선", "sports.chosun.com": "스포츠조선", "joongang.co.kr": "중앙일보",
    "donga.com": "동아일보", "asiae.co.kr": "아시아경제", "view.asiae.co.kr": "아시아경제",
    "heraldcorp.com": "헤럴드경제", "biz.heraldcorp.com": "헤럴드경제", "sedaily.com": "서울경제",
    "fnnews.com": "파이낸셜뉴스", "zdnet.co.kr": "지디넷코리아", "etnews.com": "전자신문",
    "dt.co.kr": "디지털타임스", "inews24.com": "아이뉴스24", "bloter.net": "블로터",
    "ddaily.co.kr": "디지털데일리", "isplus.com": "일간스포츠", "sportskhan.co.kr": "스포츠경향",
    "sportsseoul.com": "스포츠서울", "tenasia.co.kr": "텐아시아", "starnewskorea.com": "스타뉴스",
    "xportsnews.com": "엑스포츠뉴스", "osen.co.kr": "OSEN", "newsen.com": "뉴스엔",
    "mydaily.co.kr": "마이데일리", "topstarnews.net": "톱스타뉴스", "slist.kr": "싱글리스트",
    "kmib.co.kr": "국민일보", "hani.co.kr": "한겨레", "khan.co.kr": "경향신문",
    "seoul.co.kr": "서울신문", "segye.com": "세계일보", "munhwa.com": "문화일보",
    "hankookilbo.com": "한국일보", "daily.hankooki.com": "데일리한국", "kukinews.com": "쿠키뉴스",
    "newdaily.co.kr": "뉴데일리", "dailian.co.kr": "데일리안", "asiatoday.co.kr": "아시아투데이",
    "ajunews.com": "아주경제", "newspim.com": "뉴스핌", "etoday.co.kr": "이투데이",
    "newsway.co.kr": "뉴스웨이", "thebell.co.kr": "더벨", "viva100.com": "브릿지경제",
    "ebn.co.kr": "EBN", "businesspost.co.kr": "비즈니스포스트", "sisajournal.com": "시사저널",
    "news.einfomax.co.kr": "연합인포맥스", "einfomax.co.kr": "연합인포맥스",
    "mtn.co.kr": "MTN", "techm.kr": "테크M", "khgames.co.kr": "경향게임스",
    "dailypop.kr": "데일리팝", "onews.tv": "열린뉴스통신", "pinpointnews.co.kr": "핀포인트뉴스",
    "wideeconomy.co.kr": "와이드경제", "bntnews.co.kr": "bnt뉴스", "newsinside.kr": "뉴스인사이드",
    "geconomy.co.kr": "지이코노미", "gokorea.kr": "공감신문", "apparelnews.co.kr": "어패럴뉴스",
    "enewstoday.co.kr": "이뉴스투데이", "ekoreanews.co.kr": "이코리아", "nocutnews.co.kr": "노컷뉴스",
    "ohmynews.com": "오마이뉴스", "ytn.co.kr": "YTN", "sbs.co.kr": "SBS", "news.sbs.co.kr": "SBS",
    "kbs.co.kr": "KBS", "news.kbs.co.kr": "KBS", "imbc.com": "MBC", "jtbc.co.kr": "JTBC",
    "mbn.co.kr": "MBN", "mediatoday.co.kr": "미디어오늘", "byline.network": "바이라인네트워크",
    "platum.kr": "플래텀", "venturesquare.net": "벤처스퀘어", "wikitree.co.kr": "위키트리",
    "insight.co.kr": "인사이트", "musicbusinessworldwide.com": "Music Business Worldwide",
    "billboard.com": "Billboard", "koreaherald.com": "The Korea Herald",
    "koreatimes.co.kr": "The Korea Times", "koreajoongangdaily.joins.com": "Korea JoongAng Daily",
}

# 중복 보도 중 대표 기사 고를 때 우선순위 (앞일수록 우선)
PRESS_PRIORITY = [
    "연합뉴스", "뉴시스", "뉴스1", "이데일리", "한국경제", "매일경제", "머니투데이", "조선일보",
    "중앙일보", "동아일보", "서울경제", "아시아경제", "헤럴드경제", "파이낸셜뉴스", "조선비즈",
    "지디넷코리아", "전자신문", "디지털타임스", "아이뉴스24", "블로터", "아주경제", "뉴스핌",
    "이투데이", "IT조선", "스포츠조선", "일간스포츠", "스포츠경향", "스포츠서울", "텐아시아",
    "스타뉴스", "엑스포츠뉴스", "OSEN", "뉴스엔", "마이데일리",
]


def press_from_domain(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    host = host.removeprefix("www.").removeprefix("m.")
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in DOMAIN_PRESS:
            return DOMAIN_PRESS[cand]
    return None
