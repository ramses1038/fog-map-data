"""나라 하나의 도시 경계 데이터 만들기 (호주·뉴질랜드부터). 한국은 build_kr.py.

입력: osmium으로 뽑은 행정 경계·해안선·다리 GeoJSONSeq (오픈스트리트맵), 나라 코드
출력: <CC>.cities.geojson.gz, <CC>.summary.json

도시 단위 규칙 (안개 냠냠 기획: 대도시는 도시 1개)
- 기본: 지방 정부 구역(admin_level 6 — 호주 LGA, 뉴질랜드 시·군(territorial authority))
- 대도시: 시내 중심에서 정한 거리 안에 대표점이 있는 구역들을 합쳐 1개 도시
  (2026-10-25 사용자 결정: 시드니·멜버른 등은 하나로, 블루마운틴 같은 근교는 따로)
- 아래 구역이 없는 주·지역(예: 호주 수도 특별 지역)은 통째로 1개 도시
- 주 안에서 어느 구역에도 속하지 않는 땅(호주 미편입 지역)은 '<주> 미편입 지역' 1개

도시마다 해상도 11 H3 칸 수(면적 탐험률의 분모). 넓은 구역은 면적으로 어림한다.
"""

import gzip
import json
import math
import os
import sys

import h3
from pyproj import Geod
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import clip_by_rect, linemerge, unary_union

RES = 11
GEOD = Geod(ellps="WGS84")
EXACT_MAX_KM2 = 1500  # 이보다 넓으면 칸을 하나하나 세지 않고 면적으로 어림
REMAINDER_MIN_KM2 = 50

# 대도시: 중심 (위도, 경도), 반경 km, 한글 이름, 영어 이름
METROS = {
    "AU": [
        ((-33.8688, 151.2093), 50, "시드니", "Sydney"),
        ((-37.8136, 144.9631), 45, "멜버른", "Melbourne"),
        ((-27.4698, 153.0251), 35, "브리즈번", "Brisbane"),
        ((-31.9523, 115.8613), 40, "퍼스", "Perth"),
        ((-34.9285, 138.6007), 30, "애들레이드", "Adelaide"),
        ((-42.8821, 147.3272), 20, "호바트", "Hobart"),
        ((-12.4634, 130.8456), 15, "다윈", "Darwin"),
    ],
    "NZ": [
        # 웰링턴 광역(웰링턴·로어헛·어퍼헛·포리루아)
        ((-41.2865, 174.7762), 20, "웰링턴", "Wellington"),
    ],
}

# 주·지역 한글 이름 (오픈스트리트맵에 없을 때)
KO_NAMES = {
    "New South Wales": "뉴사우스웨일스", "Victoria": "빅토리아", "Queensland": "퀸즐랜드",
    "Western Australia": "웨스턴오스트레일리아", "South Australia": "사우스오스트레일리아",
    "Tasmania": "태즈메이니아", "Northern Territory": "노던테리토리",
    "Australian Capital Territory": "오스트레일리아 수도 특별 지역",
    "Jervis Bay Territory": "저비스베이 특별 지역",
    "Northland": "노스랜드", "Auckland": "오클랜드", "Waikato": "와이카토",
    "Bay of Plenty": "베이오브플렌티", "Gisborne": "기즈번", "Hawke's Bay": "호크스베이",
    "Taranaki": "타라나키", "Manawatū-Whanganui": "마나와투-황가누이",
    "Wellington": "웰링턴", "Tasman": "태즈먼", "Nelson": "넬슨",
    "Marlborough": "말버러", "West Coast": "웨스트코스트", "Canterbury": "캔터베리",
    "Otago": "오타고", "Southland": "사우스랜드", "Chatham Islands": "채텀 제도",
    "Christchurch": "크라이스트처치", "Queenstown-Lakes District": "퀸스타운",
    "Dunedin": "더니든", "Hamilton City": "해밀턴", "Rotorua District": "로토루아",
    "Tauranga City": "타우랑가", "Napier City": "네이피어", "Nelson City": "넬슨",
    "Canberra": "캔버라", "Gold Coast": "골드코스트", "City of Gold Coast": "골드코스트",
    "Cairns Region": "케언스", "Sunshine Coast": "선샤인코스트", "Newcastle": "뉴캐슬",
    "City of Newcastle": "뉴캐슬", "Blue Mountains": "블루마운틴",
    "Byron Shire": "바이런베이", "Alice Springs": "앨리스스프링스",
    "Greater Geelong": "질롱", "Wollongong": "울런공",
}


def read_seq(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if line:
                yield json.loads(line)


def load_admin(path):
    feats = []
    for ft in read_seq(path):
        p = ft.get("properties", {})
        if p.get("boundary") != "administrative":
            continue
        lvl = p.get("admin_level")
        if lvl not in ("2", "4", "6"):
            continue
        try:
            g = shape(ft["geometry"])
        except Exception:
            continue
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        feats.append((lvl, p, g))
    return feats


def load_land(path):
    """해안선(육지가 왼쪽) 닫힌 고리로 육지 도형. 열린 사슬은 보고만 한다."""
    lines = []
    for ft in read_seq(path):
        g = shape(ft["geometry"])
        if isinstance(g, LineString) and len(g.coords) >= 2:
            lines.append(g)
    merged = linemerge(MultiLineString(lines))
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    polys, open_len = [], []
    for ln in parts:
        if ln.is_ring and len(ln.coords) >= 4:
            p = Polygon(ln.coords)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty:
                polys.append(p)
        else:
            open_len.append(round(ln.length, 3))
    print(f"land polygons: {len(polys)}, open chains: {len(open_len)} "
          f"(longest {max(open_len) if open_len else 0} deg)", flush=True)
    return polys, STRtree(polys), sorted(open_len)[-5:]


def load_extra(path):
    shapes = []
    for ft in read_seq(path):
        p = ft.get("properties", {})
        if p.get("bridge") in (None, "no") and p.get("man_made") not in (
                "pier", "breakwater", "groyne", "quay"):
            continue
        try:
            g = shape(ft["geometry"])
        except Exception:
            continue
        if g.is_empty:
            continue
        g = g.buffer(0.0001, cap_style="flat") if g.geom_type in (
            "LineString", "MultiLineString") else g.buffer(0)
        if not g.is_empty:
            shapes.append(g)
    print(f"bridges/piers: {len(shapes)}", flush=True)
    return shapes, STRtree(shapes)


def round_coords(obj, nd=5):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(v, nd) for v in obj]
        return [round_coords(v, nd) for v in obj]
    return obj


def polygonal(g):
    """면 부분만 (자르면 선·점이 섞인 묶음이 나올 수 있음)."""
    if g.is_empty:
        return g
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [q for q in getattr(g, "geoms", [])
             if q.geom_type in ("Polygon", "MultiPolygon") and not q.is_empty]
    return unary_union(parts) if parts else Polygon()


def area_km2(g):
    a, _ = GEOD.geometry_area_perimeter(g)
    return abs(a) / 1e6


def km_between(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(
        math.radians(lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(d))


def main(adm_path, land_path, extra_path, code, out_dir):
    feats = load_admin(adm_path)
    land_polys, land_tree, open_chains = load_land(land_path)
    extra_shapes, extra_tree = load_extra(extra_path)

    def clip_to_land(g):
        idx = land_tree.query(g)
        if len(idx) == 0:
            return g
        minx, miny, maxx, maxy = g.bounds
        pad = 0.01
        pieces = [clip_by_rect(land_polys[i], minx - pad, miny - pad, maxx + pad, maxy + pad)
                  for i in idx]
        pieces += [extra_shapes[i] for i in extra_tree.query(g)]
        land = unary_union([q for q in pieces if not q.is_empty])
        out = g.intersection(land)
        return out if not out.is_empty else g

    nation = [g for lvl, p, g in feats if lvl == "2" and (
        p.get("ISO3166-1") == code or p.get("ISO3166-1:alpha2") == code)]
    if not nation:
        raise SystemExit(f"{code} 나라 경계(admin_level 2)를 찾지 못함")
    country = unary_union(nation)

    def inside(g):
        return country.contains(g.representative_point())

    lvl4 = [(p, g) for lvl, p, g in feats if lvl == "4" and inside(g)]
    lvl6 = [(p, g) for lvl, p, g in feats if lvl == "6" and inside(g)]
    print(f"{code}: states/regions {len(lvl4)}, local areas {len(lvl6)}", flush=True)

    def en(p):
        return p.get("name:en") or p.get("name", "")

    def ko(p):
        n = en(p)
        return p.get("name:ko") or KO_NAMES.get(n) or KO_NAMES.get(p.get("name", "")) or n

    def province_of(g):
        pt = g.representative_point()
        for p4, g4 in lvl4:
            if g4.contains(pt):
                return ko(p4)
        return ""

    cities, notes = [], {"metros": {}, "whole_states": [], "remainders": [], "errors": []}

    def add_city(osm_id, name, name_en, g, province, kind):
        try:
            _add_city(osm_id, name, name_en, g, province, kind)
        except Exception as e:  # 한 도시가 잘못돼도 나머지는 만든다
            print(f"  ! {name}: {type(e).__name__}: {e}", flush=True)
            notes["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:120]}")

    def _add_city(osm_id, name, name_en, g, province, kind):
        g = polygonal(g)
        clipped = polygonal(clip_to_land(g))
        g = clipped if not clipped.is_empty else g
        if g.is_empty:
            raise ValueError("빈 도형")
        km2 = area_km2(g)
        if km2 <= EXACT_MAX_KM2:
            cells = len(h3.geo_to_cells(g.__geo_interface__, RES))
        else:
            cells = int(km2 / h3.average_hexagon_area(RES, "km^2"))
        tol = 0.0001 if km2 < 2000 else 0.0005 if km2 < 50000 else 0.002
        simple = g.simplify(tol, preserve_topology=True)
        print(f"  {name}: {cells} cells, {km2:.0f} km2", flush=True)
        cities.append({
            "type": "Feature",
            "properties": {
                "id": f"{code}-{osm_id}",
                "name": name,
                "name_en": name_en,
                "province": province,
                "kind": kind,
                "cells": cells,
                "area_km2": round(km2, 2),
            },
            "geometry": round_coords(mapping(simple)),
        })

    # 1) 대도시: 반경 안 구역 합치기
    used = set()
    for (center, radius, name_ko, name_en) in METROS.get(code, []):
        members = []
        for i, (p6, g6) in enumerate(lvl6):
            if i in used:
                continue
            pt = g6.representative_point()
            if km_between(center, (pt.y, pt.x)) <= radius:
                members.append(i)
        if not members:
            continue
        used.update(members)
        g = unary_union([lvl6[i][1] for i in members])
        first = lvl6[members[0]][0]
        add_city(f"metro-{first.get('@id', first.get('id', ''))}", name_ko, name_en, g,
                 province_of(g), "metro")
        notes["metros"][name_ko] = sorted(en(lvl6[i][0]) for i in members)

    # 2) 나머지 지방 정부 구역
    for i, (p6, g6) in enumerate(lvl6):
        if i in used:
            continue
        add_city(p6.get("@id", p6.get("id", "")), ko(p6), en(p6), g6, province_of(g6),
                 "city" if "City" in en(p6) else "county")

    # 3) 아래 구역이 없는 주 → 통째로, 구역 밖 땅이 넓으면 '미편입 지역'
    for p4, g4 in lvl4:
        inner = [g6 for p6, g6 in lvl6 if g4.contains(g6.representative_point())]
        if not inner:
            name = "캔버라" if en(p4) == "Australian Capital Territory" else ko(p4)
            add_city(p4.get("@id", p4.get("id", "")), name,
                     "Canberra" if name == "캔버라" else en(p4), g4, ko(p4), "metro")
            notes["whole_states"].append(en(p4))
            continue
        try:
            rest = polygonal(g4.difference(unary_union(inner)).buffer(0))
        except Exception as e:
            notes["errors"].append(f"{en(p4)} 미편입 계산: {e}")
            continue
        if rest.is_empty:
            continue
        km2 = area_km2(rest)
        if km2 >= REMAINDER_MIN_KM2:
            add_city(f"rest-{p4.get('@id', p4.get('id', ''))}", f"{ko(p4)} 미편입 지역",
                     f"{en(p4)} (unincorporated)", rest, ko(p4), "county")
            notes["remainders"].append(f"{en(p4)}: {km2:.0f} km2")

    cities.sort(key=lambda c: (c["properties"]["province"], c["properties"]["name"]))
    fc = {
        "type": "FeatureCollection",
        "country": code,
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "res": RES,
        "features": cities,
    }
    raw = json.dumps(fc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    os.makedirs(out_dir, exist_ok=True)
    out = f"{out_dir}/{code}.cities.geojson.gz"
    with gzip.open(out, "wb", compresslevel=9) as f:
        f.write(raw)
    summary = {
        "country": code,
        "states": sorted(ko(p) for p, _ in lvl4),
        "cities": len(cities),
        "by_kind": {k: sum(1 for c in cities if c["properties"]["kind"] == k)
                    for k in ("metro", "city", "county")},
        "metros": notes["metros"],
        "whole_states": notes["whole_states"],
        "remainders": notes["remainders"],
        "errors": notes["errors"],
        "coast_open_chains": open_chains,
        "raw_bytes": len(raw),
        "gz_bytes": os.path.getsize(out),
        "largest": sorted(((c["properties"]["name"], c["properties"]["area_km2"])
                           for c in cities), key=lambda x: -x[1])[:5],
        "no_province": [c["properties"]["name"] for c in cities
                        if not c["properties"]["province"]][:20],
    }
    with open(f"{out_dir}/{code}.summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:6])
