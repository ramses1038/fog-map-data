"""도시별 명소 고르기 (5단계-1).

입력: wiki.geojsonseq   위키백과/위키데이터 태그가 있는 장소 (점·면)
      roads.geojsonseq  산 정상 판단용 길 (차로·등산로)
      KR.cities.geojson.gz
출력: out/KR.landmarks.json.gz, out/KR.landmarks.summary.json

고르는 기준 (기획 5단계, 2026-10-23):
- 한국어 위키백과 문서가 있는 장소 중 최근 12개월 조회수 순
- 도시마다 서울 30 · 광역시 20 · 시 10 · 군 5
- 종류: 관광지·문화재/유적·박물관/미술관·사찰·공원·해수욕장·전망대·유명한 다리/건물·산 정상
- 산 정상은 안전하고 대중적인 곳만: 정상 100m 안까지 일반 등산로(위험 등급 아님)가 있거나
  300m 안에 차로가 있어야 함
- 발견: 작은 장소는 반경 100m, 넓은 장소(면)는 그 구역 안 (앱에서 판정)
"""

import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from landmark_kinds import kind_of  # noqa: E402
from shapely import STRtree
from shapely.geometry import Point, mapping, shape

# 나라별: 위키데이터 나라 번호, 후보 문서 언어, 명소를 더 많이 고를 큰 도시(이름 → 개수)
COUNTRIES = {
    "KR": {"q": "Q884", "lang": "ko", "special": {"서울특별시": 30}},
    "AU": {"q": "Q408", "lang": "en", "special": {"시드니": 30, "멜버른": 30}},
    "NZ": {"q": "Q664", "lang": "en", "special": {"오클랜드": 30}},
}
CC = "KR"


def conf():
    return COUNTRIES[CC]


UA = "fog-map-data/1.0 (personal exploration app; github.com/ramses1038/fog-map-data)"
PER_KIND = {"special": 30, "metro": 20, "city": 10, "county": 5}

MOTOR = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service", "road", "track",
}
SAFE_SAC = {None, "hiking", "mountain_hiking"}
AREA_MAX_KM2 = 30  # 이보다 넓은 면(국립공원 등)은 명소로 쓰지 않음


def category(p):
    t, h = p.get("tourism"), p.get("historic")
    # 길·철로 자체(○○로, 철교 구간 등)는 명소가 아님. 다리는 man_made=bridge 로만.
    if (p.get("highway") or p.get("railway")) and p.get("man_made") != "bridge":
        return None
    if p.get("natural") == "peak":
        return "peak"
    if p.get("natural") == "beach":
        return "beach"
    if t == "viewpoint":
        return "viewpoint"
    if t in ("museum", "gallery"):
        return "museum"
    if t in ("theme_park", "zoo", "aquarium", "attraction"):
        return "attraction"
    if p.get("amenity") == "place_of_worship" and p.get("religion") == "buddhist":
        return "temple"
    if h in ("castle", "palace", "fort", "monument", "archaeological_site", "tomb",
             "city_gate", "building", "temple", "ruins", "memorial", "heritage", "yes"):
        return "heritage"
    if p.get("leisure") in ("park", "garden", "nature_reserve") or p.get("boundary") == "national_park":
        return "park"
    if p.get("man_made") in ("bridge", "tower", "lighthouse"):
        return "structure"
    return None


def ko_title(p, qmap):
    lang = conf()["lang"]
    w = p.get("wikipedia") or ""
    if w.startswith(f"{lang}:"):
        return w[len(lang) + 1:].strip()
    t = (p.get(f"wikipedia:{lang}") or "").strip()
    if t:
        return t
    # 위키데이터 번호만 있으면 한국어 위키백과 문서 이름으로 바꾼다
    q = (p.get("wikidata") or "").strip()
    return qmap.get(q)


def wikidata_link_counts(qids):
    """위키데이터 번호 → 문서가 있는 언어판 수 (50개씩)."""
    out = {}
    for i in range(0, len(qids), 50):
        part = "|".join(qids[i:i + 50])
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               f"&props=sitelinks&ids={part}")
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    for q, e in json.load(r).get("entities", {}).items():
                        out[q] = sum(1 for k in (e.get("sitelinks") or {})
                                     if k.endswith("wiki") and k != "commonswiki")
                break
            except Exception:
                time.sleep(2 + attempt * 3)
    return out


def wikidata_ko_titles(qids):
    """위키데이터 번호 → 한국어 위키백과 문서 이름 (50개씩 묻는다)."""
    out = {}
    qids = sorted(q for q in qids if q.startswith("Q"))
    for i in range(0, len(qids), 50):
        part = "|".join(qids[i:i + 50])
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               f"&props=sitelinks&sitefilter={conf()['lang']}wiki&ids={part}")
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    ents = json.load(r).get("entities", {})
                for q, e in ents.items():
                    t = (e.get("sitelinks") or {}).get(f"{conf()['lang']}wiki", {}).get("title")
                    if t:
                        out[q] = t
                break
            except Exception:
                time.sleep(1 + attempt * 2)
    return out


def wikidata_ko_labels(qids):
    """위키데이터 번호 → 한국어 이름 (해외 명소를 한글로 보여 주려고)."""
    out = {}
    qids = sorted(q for q in qids if q and q.startswith("Q"))
    for i in range(0, len(qids), 50):
        part = "|".join(qids[i:i + 50])
        url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&format=json"
               f"&props=labels&languages=ko&ids={part}")
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=30) as r:
                    for q, e in json.load(r).get("entities", {}).items():
                        v = (e.get("labels") or {}).get("ko", {}).get("value")
                        if v:
                            out[q] = v
                break
            except Exception:
                time.sleep(1 + attempt * 2)
    return out


def read(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if line:
                yield json.loads(line)


def meters_deg(m):
    return m / 111_000


def views(title, start, end):
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/ko.wikipedia/"
           f"all-access/user/{urllib.parse.quote(title.replace(' ', '_'), safe='')}/monthly/{start}/{end}")
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return sum(i["views"] for i in json.load(r).get("items", []))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 0
            DIAG["errors"].append(f"views {e.code}")
            ra = e.headers.get("Retry-After") if e.headers else None
            time.sleep(int(ra) if ra and ra.isdigit() else 2 + attempt * 3)
        except Exception:
            time.sleep(1 + attempt * 2)
    return 0


# 위키데이터에서 직접 가져올 종류 (한국 OSM 자료에는 위키 태그가 드물어서)
WD_CLASSES = {
    "Q570116": "attraction",   # 관광지
    "Q194195": "attraction",   # 놀이공원
    "Q2281788": "attraction",  # 수족관
    "Q43501": "attraction",    # 동물원
    "Q16560": "heritage",      # 궁궐
    "Q23413": "heritage",      # 성
    "Q57821": "heritage",      # 요새·산성
    "Q381885": "heritage",     # 무덤·왕릉
    "Q5393308": "temple",      # 불교 사찰
    "Q33506": "museum",        # 박물관
    "Q207694": "museum",       # 미술관
    "Q8502": "peak",           # 산
    "Q40080": "beach",         # 해변
    "Q22698": "park",          # 공원
    "Q12280": "structure",     # 다리
    "Q12518": "structure",     # 탑·타워
    "Q39715": "structure",     # 등대
    "Q1440300": "viewpoint",   # 전망대
    "Q34038": "attraction",    # 폭포
}
SPARQL = "https://query.wikidata.org/sparql"
DIAG = {"wd": {}, "errors": []}


_last_sparql = [0.0]


def sparql(q):
    """위키데이터 질의. 깃허브 서버에서는 1분에 1번으로 제한되므로 간격을 지킨다."""
    url = SPARQL + "?format=json&query=" + urllib.parse.quote(q)
    for attempt in range(5):
        wait = 62 - (time.time() - _last_sparql[0])
        if wait > 0:
            time.sleep(wait)
        _last_sparql[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/sparql-results+json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["results"]["bindings"]
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            print(f"  sparql retry {attempt}: {e.code} retry-after={ra}", flush=True)
            DIAG["errors"].append(f"sparql {e.code}")
            if ra and ra.isdigit():
                if int(ra) > 300:
                    break  # 오래 막혔으면 기다리지 않고 넘어간다
                time.sleep(int(ra))
        except Exception as e:
            print(f"  sparql retry {attempt}: {e}", flush=True)
            DIAG["errors"].append(f"sparql: {str(e)[:80]}")
            time.sleep(5 + attempt * 10)
    return []


def wikidata_candidates():
    """한국에 있고 좌표·한국어 위키백과 문서가 있는 장소들 (종류별로 묻는다)."""
    out = {}

    def add(rows, cat):
        for r in rows:
            title = r["title"]["value"]
            wkt = r["coord"]["value"]  # Point(lon lat)
            try:
                lon, lat = map(float, wkt[wkt.index("(") + 1:wkt.index(")")].split())
            except ValueError:
                continue
            links = int(r.get("links", {}).get("value", 0) or 0)
            if title not in out:
                out[title] = {"title": title, "name": title, "cat": cat,
                              "geom": Point(lon, lat), "ele": None, "links": links,
                              "qid": r["item"]["value"].rsplit("/", 1)[-1]}

    for q, cat in WD_CLASSES.items():
        rows = sparql(f"""
SELECT ?item ?title ?coord ?links WHERE {{
  ?item wdt:P17 wd:{conf()['q']}; wdt:P625 ?coord; wdt:P31/wdt:P279* wd:{q}; wikibase:sitelinks ?links.
  ?article schema:about ?item; schema:isPartOf <https://{conf()['lang']}.wikipedia.org/>; schema:name ?title.
}}""")
        add(rows, cat)
        DIAG["wd"][q] = len(rows)
        print(f"  wikidata {q} ({cat}): {len(rows)}", flush=True)
    if CC != "KR":
        return out
    # 문화재 중 사적·명승 (국보·보물은 옮길 수 있는 유물이 많아 뺌)
    rows = sparql("""
SELECT ?item ?title ?coord ?links WHERE {
  ?item wdt:P17 wd:Q884; wdt:P625 ?coord; wdt:P1435 ?d; wikibase:sitelinks ?links.
  ?d rdfs:label ?dl. FILTER(LANG(?dl) = "ko" && (CONTAINS(?dl, "사적") || CONTAINS(?dl, "명승")))
  ?article schema:about ?item; schema:isPartOf <https://ko.wikipedia.org/>; schema:name ?title.
}""")
    add(rows, "heritage")
    print(f"  wikidata 사적·명승: {len(rows)}", flush=True)
    return out


def main(wiki_p, roads_p, cities_p, out_dir, start, end, code="KR"):
    global CC
    CC = code
    # 1) 후보
    qids = set()
    for ft in read(wiki_p):
        p = ft.get("properties", {})
        if p.get("wikidata") and category(p):
            qids.add(p["wikidata"].strip())
    qmap = wikidata_ko_titles(qids)
    print(f"wikidata ids {len(qids)} -> {conf()['lang']}wiki {len(qmap)}", flush=True)
    cands = {}
    for ft in read(wiki_p):
        p = ft.get("properties", {})
        cat = category(p)
        title = ko_title(p, qmap) if cat else None
        if not title or not cat:
            continue
        try:
            g = shape(ft["geometry"])
        except Exception:
            continue
        if g.is_empty:
            continue
        if g.geom_type in ("Polygon", "MultiPolygon"):
            if not g.is_valid:
                g = g.buffer(0)
            if g.area * 111 * 91 > AREA_MAX_KM2:
                continue
        elif g.geom_type != "Point":
            # 선(다리 등)은 가운데 점으로
            g = g.interpolate(0.5, normalized=True)
        name = p.get("name:ko") or p.get("name") or title
        old = cands.get(title)
        # 같은 문서가 여러 곳이면 면을 우선
        if old is None or (old["geom"].geom_type == "Point" and g.geom_type != "Point"):
            cands[title] = {"title": title, "name": name, "cat": cat, "geom": g,
                            "ele": p.get("ele"), "qid": (p.get("wikidata") or "").strip()}
    print(f"candidates from OSM: {len(cands)}", flush=True)
    # 위키데이터 후보 더하기 (같은 문서가 OSM에 면으로 있으면 OSM 쪽을 씀)
    # 위키데이터 직접 질의는 오픈스트리트맵에 위키 태그가 드문 한국에서만.
    # (호주·뉴질랜드는 오픈스트리트맵 후보가 충분하고, 깃허브 서버에서 질의가 자주 막힘)
    for title, c in (wikidata_candidates() if CC == "KR" else {}).items():
        if title not in cands:
            cands[title] = c
    print(f"candidates total: {len(cands)}", flush=True)
    DIAG["candidates_total"] = len(cands)

    # 2) 산 정상: 안전하고 대중적인 곳만
    peaks = [c for c in cands.values() if c["cat"] == "peak"]
    if peaks:
        motor, trail, bad = [], [], []
        for ft in read(roads_p):
            p = ft.get("properties", {})
            hw = p.get("highway")
            try:
                g = shape(ft["geometry"])
            except Exception:
                continue
            if hw in MOTOR and p.get("access") not in ("private", "no"):
                motor.append(g)
            elif hw in ("path", "footway", "steps"):
                (trail if p.get("sac_scale") in SAFE_SAC else bad).append(g)
        mt, tt = STRtree(motor), STRtree(trail)
        dropped = 0
        for c in peaks:
            pt = c["geom"].centroid
            near_road = len(mt.query(pt.buffer(meters_deg(300)), predicate="intersects")) > 0
            near_trail = len(tt.query(pt.buffer(meters_deg(100)), predicate="intersects")) > 0
            if not (near_road or near_trail):
                del cands[c["title"]]
                dropped += 1
        print(f"peaks: {len(peaks)} kept {len(peaks) - dropped}", flush=True)

    # 3) 도시 정하기
    fc = json.load(gzip.open(cities_p))
    cities = [(f["properties"], shape(f["geometry"]).buffer(0)) for f in fc["features"]]
    ctree = STRtree([g for _, g in cities])
    for c in list(cands.values()):
        pt = c["geom"].representative_point()
        hit = [i for i in ctree.query(pt) if cities[i][1].contains(pt)]
        if not hit:
            del cands[c["title"]]
            continue
        c["city"] = cities[hit[0]][0]
    print(f"in cities: {len(cands)}", flush=True)
    DIAG["in_cities"] = len(cands)

    # 4) 인기: 위키데이터에서 몇 개 언어로 문서가 있는지 (조회수는 요청 제한 때문에 너무 느려서 쓰지 않음)
    need = sorted({c["qid"] for c in cands.values() if c.get("qid") and not c.get("links")})
    counts = wikidata_link_counts(need)
    for c in cands.values():
        if not c.get("links"):
            c["links"] = counts.get(c.get("qid"), 1)  # 한국어 문서는 있으므로 최소 1
        c["views"] = 0
    DIAG["links_fetched"] = len(counts)
    if CC != "KR":
        # 해외: 한국어 이름이 있으면 한글로, 없으면 영어 그대로 (영어 이름은 따로 보관)
        ko = wikidata_ko_labels({c.get("qid") for c in cands.values()})
        for c in cands.values():
            c["name_en"] = c["name"]
            if ko.get(c.get("qid")):
                c["name"] = ko[c["qid"]]
        DIAG["ko_names"] = sum(1 for c in cands.values() if c["name"] != c["name_en"])
    print("popularity done", flush=True)

    # 5) 도시마다 고르기
    by_city = {}
    for c in cands.values():
        by_city.setdefault(c["city"]["id"], []).append(c)
    out, summary_cities = [], {}
    for props, _ in cities:
        n = conf()["special"].get(props["name"]) or PER_KIND.get(props["kind"], 10)
        # 조회수 순, 조회수를 못 받았으면 위키데이터 언어판 수로
        lst = sorted(by_city.get(props["id"], []), key=lambda c: -c.get("links", 0))
        # 이름이 같은 곳(같은 장소가 문서 두 개로 있는 경우)은 하나만
        seen, picked = set(), []
        for c in lst:
            key = c["name"].replace(" ", "")
            if key in seen:
                continue
            seen.add(key)
            picked.append(c)
            if len(picked) >= n:
                break
        summary_cities[props["name"]] = [f'{c["name"]}({c["cat"]})' for c in picked]
        for c in picked:
            g = c["geom"]
            item = {
                "id": f'{props["id"]}:{c["title"]}',
                "name": c["name"],
                "wiki": c["title"],
                "cat": c["cat"],
                "kind": kind_of(c.get("name_en") or c["name"], c["title"], c["cat"]),
                "city": props["id"],
                "links": c.get("links", 0),
            }
            if c.get("name_en") and c["name_en"] != c["name"]:
                item["name_en"] = c["name_en"]
            pt = g.representative_point() if g.geom_type != "Point" else g
            item["lat"], item["lon"] = round(pt.y, 5), round(pt.x, 5)
            if g.geom_type != "Point":
                simple = g.simplify(0.0001, preserve_topology=True)
                item["area"] = json.loads(json.dumps(mapping(simple)))  # GeoJSON 도형
            out.append(item)

    data = {"v": 1, "country": CC,
            "attribution": "© OpenStreetMap contributors (ODbL), Wikipedia",
            "landmarks": out}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(f"{out_dir}/{CC}.landmarks.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)
    cats, kinds = {}, {}
    for i in out:
        cats[i["cat"]] = cats.get(i["cat"], 0) + 1
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    summary = {
        "landmarks": len(out),
        "by_category": cats,
        "cities_with_fewer": sum(
            1 for props, _ in cities
            if len(summary_cities[props["name"]]) <
            (conf()["special"].get(props["name"]) or PER_KIND.get(props["kind"], 10))),
        "raw_bytes": len(raw),
        "diag": {**DIAG, "errors": DIAG["errors"][:10], "error_count": len(DIAG["errors"])},
        "samples": {k: summary_cities.get(k) for k in
                    ["서울특별시", "부산광역시", "광주", "순천시", "광양시", "여수시", "제주시", "울릉군",
                     "시드니", "멜버른", "브리즈번", "캔버라", "오클랜드", "퀸스타운", "크라이스트처치"]
                    if k in summary_cities},
        "by_kind": kinds,
    }
    with open(f"{out_dir}/{CC}.landmarks.summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:8])
