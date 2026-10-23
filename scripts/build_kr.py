"""한국 시·군 경계 데이터 만들기.

입력: osmium으로 뽑은 행정 경계 GeoJSONSeq (오픈스트리트맵)
출력: KR.cities.geojson.gz  (도시별 경계 + 정보)
      KR.summary.json         (검증용 요약)

도시 단위 규칙 (안개 냠냠 기획 문서 4-7):
- 특별시·광역시·특별자치시: 통째로 1개 도시 (행정 경계 4단계)
- 도·특별자치도: 그 안의 시·군 (행정 경계 6단계). 일반구(7단계)는 쓰지 않음.

도시마다 해상도 11 H3 칸 수(면적 탐험률의 분모)를 함께 계산한다.
"""

import gzip
import json
import sys

import h3
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import clip_by_rect, linemerge, unary_union

RES = 11
SIMPLIFY_DEG = 0.0001  # 약 10m


def load(path):
    feats = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            ft = json.loads(line)
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
    """오픈스트리트맵 해안선(natural=coastline) 선들로 육지 도형을 만든다.

    해안선은 육지가 왼쪽에 오도록 그려진 선이다. 섬처럼 닫힌 선은 그대로 육지가 되고,
    한반도 본토 해안선은 휴전선 근처 자료 경계에서 끊겨 있으므로
    양 끝을 북쪽(북위 40도)으로 이어 닫는다. (남한 도시를 자르는 데만 쓰므로 충분)
    """
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            g = shape(json.loads(line)["geometry"])
            if isinstance(g, LineString) and len(g.coords) >= 2:
                lines.append(g)
    merged = linemerge(MultiLineString(lines))
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]

    polys, open_chains = [], []
    for ln in parts:
        if ln.is_ring and len(ln.coords) >= 4:
            p = Polygon(ln.coords)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty:
                polys.append(p)
        else:
            open_chains.append(ln)

    if open_chains:
        main = max(open_chains, key=lambda l: l.length)
        (sx, sy), (ex, ey) = main.coords[0], main.coords[-1]
        ring = list(main.coords) + [(ex, 40.0), (sx, 40.0), (sx, sy)]
        p = Polygon(ring)
        if not p.is_valid:
            p = p.buffer(0)
        polys.append(p)
        print(f"open coastline chains: {len(open_chains)} "
              f"(main {main.length:.2f} deg, ends {main.coords[0]} {main.coords[-1]})")
    print(f"land polygons: {len(polys)}")
    return polys, STRtree(polys)


def round_coords(obj, nd=5):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(v, nd) for v in obj]
        return [round_coords(v, nd) for v in obj]
    return obj


def main(src, land_path, out_dir):
    feats = load(src)
    land_polys, land_tree = load_land(land_path)

    def clip_to_land(g):
        idx = land_tree.query(g)
        if len(idx) == 0:
            return g
        # 본토 도형은 아주 크므로 도시 주변 사각형만 잘라서 쓴다 (속도)
        minx, miny, maxx, maxy = g.bounds
        pad = 0.01
        pieces = [clip_by_rect(land_polys[i], minx - pad, miny - pad,
                               maxx + pad, maxy + pad) for i in idx]
        land = unary_union([q for q in pieces if not q.is_empty])
        out = g.intersection(land)
        return out if not out.is_empty else g
    korea = [g for lvl, p, g in feats
             if lvl == "2" and (p.get("ISO3166-1") == "KR"
                                or p.get("ISO3166-1:alpha2") == "KR")]
    if not korea:
        raise SystemExit("대한민국(admin_level 2) 경계를 찾지 못함")
    kr = korea[0]

    def in_korea(g):
        return kr.contains(g.representative_point())

    lvl4 = [(p, g) for lvl, p, g in feats if lvl == "4" and in_korea(g)]
    lvl6 = [(p, g) for lvl, p, g in feats if lvl == "6" and in_korea(g)]

    metro_suffix = ("특별시", "광역시", "특별자치시")
    cities = []
    metros = [(p, g) for p, g in lvl4
              if (p.get("name:ko") or p.get("name", "")).endswith(metro_suffix)]
    metro_ids = {id(g) for _, g in metros}
    others = [(p, g) for p, g in lvl4 if id(g) not in metro_ids]

    def add_city(p, g, province, kind):
        g = clip_to_land(g)
        cells = h3.geo_to_cells(g.__geo_interface__, RES)
        sample = list(cells)[:3000]
        mean_km2 = (sum(h3.cell_area(c, "km^2") for c in sample) / len(sample)
                    if sample else 0)
        print(f"  {p.get('name:ko') or p.get('name')}: {len(cells)} cells", flush=True)
        simple = g.simplify(SIMPLIFY_DEG, preserve_topology=True)
        cities.append({
            "type": "Feature",
            "properties": {
                "id": f"KR-{p.get('@id', p.get('id', ''))}",
                "name": p.get("name:ko") or p.get("name"),
                "name_en": p.get("name:en"),
                "province": province,
                "kind": kind,
                "cells": len(cells),
                "area_km2": round(len(cells) * mean_km2, 2),
            },
            "geometry": round_coords(mapping(simple)),
        })

    for p4, g4 in metros:
        add_city(p4, g4, p4.get("name:ko") or p4.get("name"), "metro")

    # 도 지역의 시·군: 도 경계가 없거나 깨져 있어도 빠지지 않도록
    # 시·군(6단계)을 직접 찾고, 도 이름은 포함하는 도 경계에서 가져온다.
    orphans = []
    for p6, g6 in lvl6:
        n6 = p6.get("name:ko") or p6.get("name", "")
        if not (n6.endswith("시") or n6.endswith("군")):
            continue
        pt = g6.representative_point()
        if any(gm.contains(pt) for _, gm in metros):
            continue  # 광역시 안의 군·구는 광역시에 포함
        province = next((p4.get("name:ko") or p4.get("name")
                         for p4, g4 in others if g4.contains(pt)), None)
        if province is None:
            province = p6.get("is_in:province") or ""
            orphans.append(n6)
        add_city(p6, g6, province, "city" if n6.endswith("시") else "county")
    unmatched_provinces = orphans

    cities.sort(key=lambda c: (c["properties"]["province"], c["properties"]["name"]))
    fc = {
        "type": "FeatureCollection",
        "country": "KR",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "res": RES,
        "features": cities,
    }
    raw = json.dumps(fc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(f"{out_dir}/KR.cities.geojson.gz", "wb", compresslevel=9) as f:
        f.write(raw)

    by_name = {c["properties"]["name"]: c["properties"] for c in cities}
    samples = {}
    for n in ["서울특별시", "부산광역시", "순천시", "광양시", "목포시", "해남군",
              "제주시", "서귀포시", "수원시", "세종특별자치시", "신안군", "울릉군"]:
        if n in by_name:
            samples[n] = by_name[n]["area_km2"]
    summary = {
        "provinces": len(lvl4),
        "cities": len(cities),
        "metro": sum(1 for c in cities if c["properties"]["kind"] == "metro"),
        "city": sum(1 for c in cities if c["properties"]["kind"] == "city"),
        "county": sum(1 for c in cities if c["properties"]["kind"] == "county"),
        "province_names": sorted({p.get("name:ko") or p.get("name") for p, _ in lvl4}),
        "cities_without_province": unmatched_provinces,
        "approx_km2": samples,
        "total_cells": sum(c["properties"]["cells"] for c in cities),
        "gz_bytes": None,
    }
    import os
    summary["gz_bytes"] = os.path.getsize(f"{out_dir}/KR.cities.geojson.gz")
    summary["raw_bytes"] = len(raw)
    with open(f"{out_dir}/KR.summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
