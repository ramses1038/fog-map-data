"""명소를 아이콘 13가지로 나눈다 (기획 5단계 명소 아이콘, 2026-10-24 사용자 확정).

입력: 지금 분류(cat, 9가지) + 이름(name) + 위키백과 문서 이름(wiki) + (있으면) 위키데이터 종류 이름들.
이름 끝말과 들어 있는 낱말로 먼저 나누고, 남은 것은 지금 분류로 정한다.

  history  역사적인 장소 (궁궐·성곽·옛 건물·왕릉·유적지)
  temple   사찰 (옛 석탑·부도·당간지주 포함)
  museum   박물관·미술관·공연장
  peak     산 정상
  water    바다·해변·호수
  falls    폭포·계곡
  cave     동굴
  island   섬
  park     공원·수목원·정원
  bridge   다리
  tower    타워·탑·전망대
  modern   근대 건축
  other    기타 관광지
"""
import re

KINDS = ["history", "temple", "museum", "peak", "water", "falls", "cave",
         "island", "park", "bridge", "tower", "modern", "other"]

# 위키데이터 종류 이름(영어 라벨)에 들어 있으면 그 분류로 (CI에서 받아 올 때만 씀)
WD_HINTS = [
    ("cave", ("cave", "lava tube")),
    ("falls", ("waterfall", "valley", "gorge", "canyon")),
    ("island", ("island", "islet")),
    ("water", ("lake", "reservoir", "beach", "lagoon", "bay")),
    ("bridge", ("bridge",)),
    ("museum", ("museum", "gallery", "theatre", "theater", "concert hall",
                "performing arts", "opera house", "planetarium")),
    ("modern", ("church building", "cathedral", "bank building",
                "railway station building", "former bank")),
]


def _has(s, *words):
    return any(w in s for w in words)


def kind_of(name, wiki, cat, wd_types=()):
    n = f"{name} {wiki}".replace("(", " ").replace(")", " ")
    first = name.split("(")[0].strip()

    # 1) 절 안의 탑·부도는 사찰 (사용자 결정: 석탑은 사찰)
    if _has(n, "석탑", "층탑", "전탑", "사리탑", "승탑", "부도", "당간", "석등", "사지 ", "사지", "절터",
            "다보탑", "석가탑", "대웅전", "극락전", "마애"):
        return "temple"
    if cat == "temple":
        return "temple"
    if cat == "peak" and not _has(n, "폭포", "계곡", "동굴"):
        return "peak"
    # 절 이름 (○○사, ○○암). 청사·관사·역사·사무소 같은 건물은 뺀다
    if (re.search(r"[가-힣]{1,4}(사|암)$", first) and cat in ("temple", "attraction", "heritage")
            and not re.search(r"(청사|관사|역사|당사|회사|공사)$", first)):
        return "temple"
    # 2) 이름이 확실한 것
    if _has(n, "동굴", "용암굴") or re.search(r"굴$", first):
        return "cave"
    if _has(n, "폭포", "계곡", "구곡", "협곡"):
        return "falls"
    if re.search(r"섬$", first) or _has(n, "섬 "):
        return "island"
    if (_has(n, "해수욕장", "해변", "해안", "호수", "저수지", "바닷가", "늪", "주상절리")
            or re.search(r"[가-힣]호$", first)):
        return "water"
    if _has(n, "능선", "봉우리"):
        return "peak"
    if _has(n, "박물관", "미술관", "기념관", "전시관", "과학관", "역사관", "문학관",
            "공연장", "극장", "아트센터", "예술의전당", "문화회관", "아트홀", "콘서트홀", "천문대"):
        return "museum"
    if _has(n, "바위") and cat in ("viewpoint", "peak"):
        return "peak"
    if _has(n, "급수탑", "기념각"):
        return "modern"
    if _has(n, "타워", "전망대", "등대", "스카이") or re.search(r"탑$", first):
        return "tower"
    if _has(n, "대교", "철교", "다리", "구름다리", "출렁다리") or (cat == "structure" and re.search(r"교$", first)):
        return "bridge"
    if (_has(n, "성당", "교회", "급수탑", "은행", "우체국", "관사", "청사", "노동당사",
             "세관", "근대", "공회당", "학교 본관", "적산가옥", "형무소")
            or re.search(r"(^|\s)(구|옛)\s", n) or re.search(r"역사$", first)):
        return "modern"
    # 옛 왕릉·성·유적 (공원 안에 있어도 역사적인 장소)
    # '릉'은 왕릉 이름 끝에서만 (강릉 같은 도시 이름 제외)
    if (_has(n, "왕릉", "산성", "읍성", "토성", "향교", "서원", "유적", "지석묘", "고인돌", "행궁",
             "고택", "묘역", "장대", "사직단", "봉수", "민속마을", "관아", "암각화",
             "각석", "고분", "문화재단지")
            or re.search(r"(궁|성|묘|단|전|릉|제|아문)$", first)):
        return "history"
    if _has(n, "수목원", "식물원", "정원", "공원", "숲", "원림", "별서", "소쇄원"):
        return "park"
    # 3) 위키데이터 종류
    low = " ".join(t.lower() for t in wd_types)
    for k, words in WD_HINTS:
        if any(w in low for w in words):
            return k
    # 4) 지금 분류로
    return {
        "heritage": "history",
        "temple": "temple",
        "museum": "museum",
        "peak": "peak",
        "beach": "water",
        "park": "park",
        "viewpoint": "tower",
        "structure": "tower",
        "attraction": "other",
    }.get(cat, "other")
