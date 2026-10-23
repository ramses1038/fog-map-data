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
from shapely.geometry import mapping, shape

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


def round_coords(obj, nd=5):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(v, nd) for v in obj]
        return [round_coords(v, nd) for v in obj]
    return obj


def main(src, out_dir):
    feats = load(src)
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
    unmatched_provinces = []

    def add_city(p, g, province, kind):
        cells = len(h3.geo_to_cells(g.__geo_interface__, RES))
        simple = g.simplify(SIMPLIFY_DEG, preserve_topology=True)
        cities.append({
            "type": "Feature",
            "properties": {
                "id": f"KR-{p.get('@id', p.get('id', ''))}",
                "name": p.get("name:ko") or p.get("name"),
                "name_en": p.get("name:en"),
                "province": province,
                "kind": kind,
                "cells": cells,
            },
            "geometry": round_coords(mapping(simple)),
        })

    for p4, g4 in lvl4:
        pname = p4.get("name:ko") or p4.get("name", "")
        if pname.endswith(metro_suffix):
            add_city(p4, g4, pname, "metro")
            continue
        found = 0
        for p6, g6 in lvl6:
            n6 = p6.get("name:ko") or p6.get("name", "")
            if not (n6.endswith("시") or n6.endswith("군")):
                continue
            if g4.contains(g6.representative_point()):
                add_city(p6, g6, pname, "city" if n6.endswith("시") else "county")
                found += 1
        if found == 0:
            unmatched_provinces.append(pname)

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

    cell_km2 = h3.average_hexagon_area(RES, unit="km^2")
    by_name = {c["properties"]["name"]: c["properties"] for c in cities}
    samples = {}
    for n in ["서울특별시", "부산광역시", "순천시", "광양시", "목포시", "해남군",
              "제주시", "서귀포시", "수원시", "세종특별자치시"]:
        if n in by_name:
            samples[n] = round(by_name[n]["cells"] * cell_km2, 1)
    summary = {
        "provinces": len(lvl4),
        "cities": len(cities),
        "metro": sum(1 for c in cities if c["properties"]["kind"] == "metro"),
        "city": sum(1 for c in cities if c["properties"]["kind"] == "city"),
        "county": sum(1 for c in cities if c["properties"]["kind"] == "county"),
        "unmatched_provinces": unmatched_provinces,
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
    main(sys.argv[1], sys.argv[2])
