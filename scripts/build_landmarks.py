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

from shapely import STRtree
from shapely.geometry import Point, mapping, shape

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
    if p.get("man_made") in ("bridge", "tower", "lighthouse") or p.get("bridge") == "yes" and p.get("name"):
        return "structure"
    if p.get("building") and t:
        return "attraction"
    return None


def ko_title(p):
    w = p.get("wikipedia") or ""
    if w.startswith("ko:"):
        return w[3:].strip()
    return (p.get("wikipedia:ko") or "").strip() or None


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
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return sum(i["views"] for i in json.load(r).get("items", []))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 0
            time.sleep(1 + attempt * 2)
        except Exception:
            time.sleep(1 + attempt * 2)
    return 0


def main(wiki_p, roads_p, cities_p, out_dir, start, end):
    # 1) 후보
    cands = {}
    for ft in read(wiki_p):
        p = ft.get("properties", {})
        title, cat = ko_title(p), category(p)
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
                            "ele": p.get("ele")}
    print(f"candidates: {len(cands)}", flush=True)

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

    # 4) 조회수
    titles = list(cands)
    with ThreadPoolExecutor(12) as ex:
        for t, v in zip(titles, ex.map(lambda t: views(t, start, end), titles)):
            cands[t]["views"] = v
    print("views done", flush=True)

    # 5) 도시마다 고르기
    by_city = {}
    for c in cands.values():
        by_city.setdefault(c["city"]["id"], []).append(c)
    out, summary_cities = [], {}
    for props, _ in cities:
        kind = "special" if props["name"] == "서울특별시" else props["kind"]
        n = PER_KIND.get(kind, 10)
        lst = sorted(by_city.get(props["id"], []), key=lambda c: -c["views"])
        picked = [c for c in lst if c["views"] > 0][:n]
        summary_cities[props["name"]] = [f'{c["name"]}({c["cat"]})' for c in picked]
        for c in picked:
            g = c["geom"]
            item = {
                "id": f'{props["id"]}:{c["title"]}',
                "name": c["name"],
                "wiki": c["title"],
                "cat": c["cat"],
                "city": props["id"],
                "views": c["views"],
            }
            pt = g.representative_point() if g.geom_type != "Point" else g
            item["lat"], item["lon"] = round(pt.y, 5), round(pt.x, 5)
            if g.geom_type != "Point":
                simple = g.simplify(0.0001, preserve_topology=True)
                item["area"] = json.loads(json.dumps(mapping(simple)))  # GeoJSON 도형
            out.append(item)

    data = {"v": 1, "country": "KR",
            "attribution": "© OpenStreetMap contributors (ODbL), Wikipedia",
            "landmarks": out}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(f"{out_dir}/KR.landmarks.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)
    cats = {}
    for i in out:
        cats[i["cat"]] = cats.get(i["cat"], 0) + 1
    summary = {
        "landmarks": len(out),
        "by_category": cats,
        "cities_with_fewer": sum(
            1 for props, _ in cities
            if len(summary_cities[props["name"]]) <
            PER_KIND.get("special" if props["name"] == "서울특별시" else props["kind"], 10)),
        "raw_bytes": len(raw),
        "samples": {k: summary_cities.get(k) for k in
                    ["서울특별시", "부산광역시", "광주", "순천시", "광양시", "여수시", "제주시", "울릉군"]},
    }
    with open(f"{out_dir}/KR.landmarks.summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:7])
