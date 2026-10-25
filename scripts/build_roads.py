"""도시별 '길 칸' 목록 만들기 (길 탐험률의 분모).

입력: roads.geojsonseq (osmium으로 뽑은 길·철도 선), KR.cities.geojson.gz (도시 경계)
출력: out/<나라>-<id>.roads.gz  도시 하나의 길 칸 목록 (한국·호주·뉴질랜드 공통)
         (H3 해상도 11 칸 번호를 정렬해 앞 칸과의 차이를 8바이트씩 적고 gzip)
      out/KR.roads.json      도시마다 길 칸 수

어떤 길을 세나 (사용자 결정 2026-10-23):
- 차도 + 사람이 다니는 길 (인도·산책로·공원길·자전거길), 고속도로·철도 포함
- 빼는 것: 등산로·산길(sac_scale), 들어갈 수 없는 사유지(access=private/no),
  아파트·공장 안 진입로·주차장 통로(service=driveway/parking_aisle 등), 농로(track),
  철도의 지하 구간·차량기지·지선, 지하철
"""

import gzip
import json
import sys

import h3
import numpy as np
from shapely import STRtree, segmentize
from shapely.geometry import shape
from shapely.prepared import prep

RES = 11
STEP_DEG = 0.00008  # 약 8m 간격으로 점을 찍어 칸을 모은다

ROAD = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "road", "service", "pedestrian", "footway",
    "steps", "cycleway", "path",
}
RAIL = {"rail", "light_rail", "narrow_gauge", "tram"}
BAD_SERVICE = {"driveway", "parking_aisle", "drive-through", "emergency_access"}
NO_ENTRY = {"private", "no"}


def keep(p):
    if p.get("access") in NO_ENTRY or p.get("indoor") == "yes":
        return False
    hw = p.get("highway")
    if hw in ROAD:
        if p.get("foot") in NO_ENTRY and hw in ("footway", "path", "steps", "pedestrian"):
            return False
        if hw == "service" and p.get("service") in BAD_SERVICE:
            return False
        if hw == "path" and (p.get("sac_scale") or p.get("informal") == "yes"):
            return False  # 등산로·산길
        if hw == "footway" and p.get("sac_scale"):
            return False
        return True
    rw = p.get("railway")
    if rw in RAIL:
        if p.get("tunnel") not in (None, "no") or p.get("service"):
            return False
        if p.get("usage") in ("industrial", "military"):
            return False
        return True
    return False


def load_roads(path):
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            ft = json.loads(line)
            if not keep(ft.get("properties", {})):
                continue
            try:
                g = shape(ft["geometry"])
            except Exception:
                continue
            if not g.is_empty:
                lines.append(g)
    print(f"roads kept: {len(lines)}", flush=True)
    return lines


def cells_of(geom, out):
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type != "LineString":
            continue
        for x, y in segmentize(part, STEP_DEG).coords:
            out.add(h3.latlng_to_cell(y, x, RES))


def main(roads_path, cities_path, out_dir):
    lines = load_roads(roads_path)
    tree = STRtree(lines)
    fc = json.load(gzip.open(cities_path))
    counts = {}
    for i, f in enumerate(fc["features"]):
        cid = f["properties"]["id"]
        city = shape(f["geometry"])
        if not city.is_valid:
            city = city.buffer(0)
        pc = prep(city)
        cells = set()
        for j in tree.query(city):
            ln = lines[j]
            if pc.contains(ln):
                cells_of(ln, cells)
            elif pc.intersects(ln):
                cells_of(ln.intersection(city), cells)
        ids = np.array(sorted(h3.str_to_int(c) for c in cells), dtype=np.uint64)
        deltas = np.diff(ids, prepend=np.uint64(0)) if len(ids) else ids
        with gzip.open(f"{out_dir}/{cid}.roads.gz", "wb", compresslevel=9) as w:
            w.write(deltas.astype("<u8").tobytes())
        counts[cid] = len(ids)
        print(f"  {i + 1}/{len(fc['features'])} {f['properties']['name']}: "
              f"{len(ids)} road cells", flush=True)
    code = fc.get("country", "KR")
    with open(f"{out_dir}/{code}.roads.json", "w", encoding="utf-8") as w:
        json.dump({"res": RES, "format": "uint64-delta-le-gzip", "cities": counts}, w)
    by_name = {f["properties"]["name"]: counts[f["properties"]["id"]] for f in fc["features"]}
    sample = {n: by_name.get(n) for n in ["서울특별시", "광양시", "순천시", "신안군", "울릉군"]}
    print(json.dumps({"cities": len(counts), "total": sum(counts.values()),
                      "sample": sample}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
