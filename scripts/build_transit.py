"""이동 보정용 자료 만들기 (4단계): 철도망·역·터널·지하 보행 통로.

입력: rail.geojsonseq (철도 선), stations.geojsonseq (역 점),
      tunnels.geojsonseq (터널 선), under.geojsonseq (보행 통로 후보 선)
출력: out/KR.transit.json.gz
  {
    "rail": {"nodes": [[lat, lon], ...],          # 갈림길·끝점
             "edges": [[u, v, 길이m, [[lat, lon], ...]], ...]},   # 갈림길 사이 선로
    "stations": [{"name", "lat", "lon", "subway": bool, "node": 가까운 갈림길 번호}],
    "tunnels": [{"kind": "road"|"rail", "len": m, "coords": [[lat, lon], ...]}],
    "under": [[[lat, lon], ...], ...]             # 지하 보행 통로 (지하상가·지하보도)
  }
  out/KR.transit.summary.json  (검증용: 개수, 지하상가 지역별 통로 길이)

규칙 (기획 4단계):
- 철도망: rail·light_rail·subway·narrow_gauge·monorail, 차량기지·지선(service=*)과 폐선 제외.
  지하철 경로 찾기와 기차 보정에 같이 쓴다. (수도권 1호선처럼 일반 철로를 달리는 노선이 있어서)
- 터널: 차도 터널과 지상 철도 터널만. 지하철 노선은 넣지 않는다. (지하철과 헷갈리지 않게)
- 지하 보행 통로: footway·corridor·pedestrian·steps·path 중 지하(layer<0, level<0,
  tunnel=yes, location=underground)인 것.
"""

import gzip
import json
import math
import sys
from collections import defaultdict

RAIL = {"rail", "light_rail", "subway", "narrow_gauge", "monorail"}
TUNNEL_ROAD = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "road", "service",
}
WALK = {"footway", "corridor", "pedestrian", "steps", "path"}
ND = 5  # 좌표 소수 자리 (약 1m)


def read(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if line:
                yield json.loads(line)


def lines_of(geom):
    t = geom["type"]
    if t == "LineString":
        return [geom["coordinates"]]
    if t == "MultiLineString":
        return geom["coordinates"]
    if t == "Polygon":
        return geom["coordinates"][:1]
    if t == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    return []


def meters(a, b):
    """a, b = (lon, lat)"""
    lat = math.radians((a[1] + b[1]) / 2)
    dx = math.radians(b[0] - a[0]) * math.cos(lat) * 6371008.8
    dy = math.radians(b[1] - a[1]) * 6371008.8
    return math.hypot(dx, dy)


def length(coords):
    return sum(meters(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def ll(coords):
    """(lon, lat) 목록 → [lat, lon] (반올림)"""
    return [[round(y, ND), round(x, ND)] for x, y in coords]


def num(v):
    try:
        return float(str(v).split(";")[0])
    except ValueError:
        return 0.0


def underground(p):
    return (num(p.get("layer", 0)) < 0 or num(p.get("level", 0)) < 0
            or p.get("tunnel") in ("yes", "building_passage")
            or p.get("location") == "underground")


# ---------------- 철도망 ----------------

def build_rail(path):
    ways = []
    for ft in read(path):
        p = ft.get("properties", {})
        if p.get("railway") not in RAIL or p.get("service"):
            continue
        if p.get("usage") in ("industrial", "military", "test"):
            continue
        for c in lines_of(ft["geometry"]):
            if len(c) >= 2:
                ways.append([(round(x, 6), round(y, 6)) for x, y in c])
    # 여러 선이 공유하는 점 = 갈림길
    use = defaultdict(int)
    for w in ways:
        for i, pt in enumerate(w):
            use[pt] += 2 if i in (0, len(w) - 1) else 1
    node_id = {}
    nodes = []

    def nid(pt):
        if pt not in node_id:
            node_id[pt] = len(nodes)
            nodes.append([round(pt[1], ND), round(pt[0], ND)])
        return node_id[pt]

    edges = []
    for w in ways:
        start = 0
        for i in range(1, len(w)):
            if i == len(w) - 1 or use[w[i]] > 1:
                seg = w[start:i + 1]
                if len(seg) >= 2:
                    edges.append([nid(seg[0]), nid(seg[-1]), round(length(seg)),
                                  ll(seg)])
                start = i
    return nodes, edges


def snap_stations(stations, nodes, edges, max_m=300):
    """역을 가장 가까운 선로 점에 붙이고, 그 점에서 선로를 잘라 갈림길(노드)로 만든다.
    (갈림길만 노드로 두면 역 대부분이 노드에서 멀어 경로를 찾을 수 없음)"""
    grid = defaultdict(list)  # 칸 → (edge 번호, 점 번호)
    for ei, e in enumerate(edges):
        for vi, (lat, lon) in enumerate(e[3]):
            grid[(int(lon / 0.005), int(lat / 0.005))].append((ei, vi))
    splits = defaultdict(set)  # edge → 자를 점 번호들
    target = []
    for st in stations:
        best, bd = None, max_m
        gx, gy = int(st["lon"] / 0.005), int(st["lat"] / 0.005)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for ei, vi in grid.get((gx + dx, gy + dy), ()):
                    lat, lon = edges[ei][3][vi]
                    d = meters((st["lon"], st["lat"]), (lon, lat))
                    if d < bd:
                        best, bd = (ei, vi), d
        target.append(best)
        if best and 0 < best[1] < len(edges[best[0]][3]) - 1:
            splits[best[0]].add(best[1])
    # 선로 자르기
    vertex_node = {}  # (edge, 점) → 노드
    new_edges = []
    for ei, (u, v, _, coords) in enumerate(edges):
        cuts = sorted(splits.get(ei, ()))
        idx = [0] + cuts + [len(coords) - 1]
        ids = [u]
        for c in cuts:
            nodes.append(coords[c])
            ids.append(len(nodes) - 1)
            vertex_node[(ei, c)] = len(nodes) - 1
        ids.append(v)
        vertex_node[(ei, 0)] = u
        vertex_node[(ei, len(coords) - 1)] = v
        for k in range(len(idx) - 1):
            part = coords[idx[k]:idx[k + 1] + 1]
            new_edges.append([ids[k], ids[k + 1],
                              round(length([(x, y) for y, x in part])), part])
    for st, t in zip(stations, target):
        st["node"] = vertex_node.get(t) if t else None
    return new_edges


def nearest_node(nodes, grid, lat, lon, max_m=400):
    best, bd = None, max_m
    gx, gy = int(lon / 0.01), int(lat / 0.01)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for i in grid.get((gx + dx, gy + dy), ()):
                d = meters((lon, lat), (nodes[i][1], nodes[i][0]))
                if d < bd:
                    best, bd = i, d
    return best


def build_stations(path, nodes):
    grid = defaultdict(list)
    for i, (lat, lon) in enumerate(nodes):
        grid[(int(lon / 0.01), int(lat / 0.01))].append(i)
    out, seen = [], set()
    for ft in read(path):
        p = ft.get("properties", {})
        if ft["geometry"]["type"] != "Point":
            continue
        is_station = p.get("railway") in ("station", "halt") or (
            p.get("public_transport") == "station"
            and (p.get("train") == "yes" or p.get("subway") == "yes"
                 or p.get("light_rail") == "yes"))
        if not is_station:
            continue
        name = p.get("name:ko") or p.get("name")
        if not name:
            continue
        lon, lat = ft["geometry"]["coordinates"]
        key = (name, round(lat, 3), round(lon, 3))
        if key in seen:
            continue
        seen.add(key)
        subway = (p.get("station") in ("subway", "light_rail") or p.get("subway") == "yes"
                  or p.get("light_rail") == "yes")
        out.append({"name": name, "lat": round(lat, ND), "lon": round(lon, ND),
                    "subway": subway, "node": nearest_node(nodes, grid, lat, lon)})
    return out


# ---------------- 터널 ----------------

def build_tunnels(path):
    out = []
    for ft in read(path):
        p = ft.get("properties", {})
        if p.get("tunnel") in (None, "no", "building_passage", "culvert"):
            continue
        hw, rw = p.get("highway"), p.get("railway")
        if hw in TUNNEL_ROAD:
            kind = "road"
        elif rw in ("rail", "light_rail", "narrow_gauge") and not p.get("service"):
            kind = "rail"
        else:
            continue  # 지하철·보행 통로 등은 제외
        for c in lines_of(ft["geometry"]):
            ln = length(c)
            if ln >= 100:  # 아주 짧은 지하차도 등은 GPS가 끊기지 않으므로 제외
                out.append({"kind": kind, "len": round(ln), "coords": ll(c)})
    return out


# ---------------- 지하 보행 통로 ----------------

DIAG = {"부산 서면": (35.1577, 129.0592), "대구 반월당": (35.8655, 128.5935),
        "광주 금남로": (35.1490, 126.9170),
        "강남역": (37.4979, 127.0276), "잠실": (37.5133, 127.1001)}
diag = defaultdict(lambda: defaultdict(int))


def build_under(path):
    out = []
    for ft in read(path):
        p = ft.get("properties", {})
        under = underground(p)
        if under:  # 진단: 지하상가 지역의 지하 선들이 어떤 태그인지
            for name, (lat, lon) in DIAG.items():
                c0 = lines_of(ft["geometry"])
                if c0 and meters((lon, lat), c0[0][0]) < 600:
                    key = "|".join(f"{k}={p.get(k)}" for k in
                                   ("highway", "indoor", "railway", "building", "area")
                                   if p.get(k))
                    diag[name][key or "(태그 없음)"] += 1
        walk = p.get("highway") in WALK or p.get("indoor") in ("corridor", "area")
        if not walk or not under:
            continue
        if p.get("access") in ("private", "no"):
            continue
        for c in lines_of(ft["geometry"]):
            if len(c) >= 2:
                out.append(ll(c))
    return out


CHECK = {  # 지하상가가 얼마나 그려졌는지 볼 곳 (반경 600m)
    "강남역": (37.4979, 127.0276), "명동": (37.5636, 126.9850),
    "을지로": (37.5660, 126.9910), "시청·서울광장": (37.5657, 126.9769),
    "고속터미널": (37.5049, 127.0049), "영등포": (37.5159, 126.9076),
    "잠실": (37.5133, 127.1001), "부산 서면": (35.1577, 129.0592),
    "부산 남포동": (35.0979, 129.0349), "대구 반월당": (35.8655, 128.5935),
    "인천 부평": (37.4895, 126.7245), "대전 중앙로": (36.3289, 127.4276),
    "광주 금남로": (35.1490, 126.9170),
}


def main(rail_p, st_p, tun_p, under_p, out_dir):
    nodes, edges = build_rail(rail_p)
    print(f"rail nodes {len(nodes)} edges {len(edges)}", flush=True)
    stations = build_stations(st_p, nodes)
    edges = snap_stations(stations, nodes, edges)
    tunnels = build_tunnels(tun_p)
    under = build_under(under_p)
    data = {"v": 1, "country": "KR", "attribution": "© OpenStreetMap contributors (ODbL)",
            "rail": {"nodes": nodes, "edges": edges}, "stations": stations,
            "tunnels": tunnels, "under": under}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(f"{out_dir}/KR.transit.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)

    def under_m_near(lat, lon, r=600):
        tot = 0.0
        for c in under:
            for i in range(len(c) - 1):
                a, b = c[i], c[i + 1]
                mid = ((a[1] + b[1]) / 2, (a[0] + b[0]) / 2)
                if meters((lon, lat), mid) <= r:
                    tot += meters((a[1], a[0]), (b[1], b[0]))
        return round(tot)

    summary = {
        "rail_km": round(sum(e[2] for e in edges) / 1000),
        "rail_edges": len(edges),
        "stations": len(stations),
        "subway_stations": sum(s["subway"] for s in stations),
        "stations_without_track": sum(s["node"] is None for s in stations),
        "tunnels_road": sum(t["kind"] == "road" for t in tunnels),
        "tunnels_rail": sum(t["kind"] == "rail" for t in tunnels),
        "tunnel_longest_m": max((t["len"] for t in tunnels), default=0),
        "under_ways": len(under),
        "under_km": round(sum(length([(x, y) for y, x in c]) for c in under) / 1000, 1),
        "under_m_near": {k: under_m_near(*v) for k, v in CHECK.items()},
        "diag_underground_tags": {k: dict(sorted(v.items(), key=lambda x: -x[1])[:12])
                                  for k, v in diag.items()},
        "gz_bytes": None,
        "raw_bytes": len(raw),
    }
    import os
    summary["gz_bytes"] = os.path.getsize(f"{out_dir}/KR.transit.json.gz")
    with open(f"{out_dir}/KR.transit.summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:6])
