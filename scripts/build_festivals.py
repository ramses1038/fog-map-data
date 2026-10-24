"""한국 축제 목록 받기 (5단계-3). 한국관광공사 TourAPI (국문 관광정보 서비스_GW).

사용 키는 깃허브 비밀 설정 TOUR_API_KEY 에만 있다 (앱에도 공개 저장소에도 넣지 않음).
출력: out/KR.festivals.json.gz  — 진행 중이거나 앞으로 1년 안에 시작하는 축제
      out/KR.festivals.summary.json

쓰는 정보: 이름·기간·좌표·주소·분류·관광공사 콘텐츠 번호. 사진은 쓰지 않는다.
"""

import datetime as dt
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASES = [
    "https://apis.data.go.kr/B551011/KorService2/searchFestival2",
    "https://apis.data.go.kr/B551011/KorService1/searchFestival1",
]
UA = "fog-map-data/1.0"


def fetch(base, key, start, page):
    q = {
        "serviceKey": key,
        "MobileOS": "AND",
        "MobileApp": "AnGaeNyamNyam",
        "_type": "json",
        "arrange": "A",
        "numOfRows": "500",
        "pageNo": str(page),
        "eventStartDate": start,
    }
    url = base + "?" + urllib.parse.urlencode(q)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # 키 오류 등은 XML로 온다
                return {"error": raw[:300]}
        except Exception as e:
            err = str(e)[:200]
            time.sleep(3 + attempt * 5)
    return {"error": err}


def items_of(resp):
    body = (resp.get("response") or {}).get("body") or {}
    items = (body.get("items") or {})
    if isinstance(items, str):
        return [], int(body.get("totalCount") or 0)
    it = items.get("item") or []
    if isinstance(it, dict):
        it = [it]
    return it, int(body.get("totalCount") or 0)


def main(out_dir):
    key = os.environ.get("TOUR_API_KEY", "").strip()
    if not key:
        raise SystemExit("TOUR_API_KEY 비밀 설정이 없어요")
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    # 이미 시작했지만 아직 안 끝난 축제도 받으려고 넉 달 전부터 시작한 것까지 묻는다
    start = (today - dt.timedelta(days=120)).strftime("%Y%m%d")

    used, rows, diag = None, [], {}
    for base in BASES:
        first = fetch(base, key, start, 1)
        if "error" in first or (first.get("response", {}).get("header", {})
                                .get("resultCode") not in ("0000", "00", None)):
            diag[base.rsplit("/", 2)[-2]] = str(first.get("error") or first.get("response", {}).get("header"))[:300]
            continue
        items, total = items_of(first)
        rows.extend(items)
        page = 2
        while len(rows) < total and page < 40:
            more, _ = items_of(fetch(base, key, start, page))
            if not more:
                break
            rows.extend(more)
            page += 1
        used = base
        break
    if used is None:
        print(json.dumps({"error": diag}, ensure_ascii=False))
        raise SystemExit("축제 목록을 받지 못했어요")

    limit = today + dt.timedelta(days=365)
    out, seen = [], set()
    for r in rows:
        try:
            s = dt.datetime.strptime(str(r.get("eventstartdate")), "%Y%m%d").date()
            e = dt.datetime.strptime(str(r.get("eventenddate")), "%Y%m%d").date()
            lon, lat = float(r.get("mapx")), float(r.get("mapy"))
        except (TypeError, ValueError):
            continue
        if e < today or s > limit or not (33 < lat < 39 and 124 < lon < 132):
            continue
        cid = str(r.get("contentid"))
        if cid in seen:
            continue
        seen.add(cid)
        out.append({
            "id": cid,
            "name": (r.get("title") or "").strip(),
            "start": s.isoformat(),
            "end": e.isoformat(),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "addr": (r.get("addr1") or "").strip(),
            "cat": r.get("cat3") or r.get("lclsSystm3") or "",
            "area": r.get("areacode") or r.get("lDongRegnCd") or "",
        })
    out.sort(key=lambda f: (f["start"], f["name"]))
    data = {"v": 1, "country": "KR", "updated": today.isoformat(),
            "source": "한국관광공사 TourAPI (공공데이터포털)", "festivals": out}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(f"{out_dir}/KR.festivals.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)
    ongoing = [f for f in out if f["start"] <= today.isoformat()]
    summary = {
        "api": used.rsplit("/", 2)[-2],
        "fetched": len(rows),
        "festivals": len(out),
        "ongoing": len(ongoing),
        "upcoming": len(out) - len(ongoing),
        "raw_bytes": len(raw),
        "sample_ongoing": [f'{f["name"]} ({f["start"]}~{f["end"]}, {f["addr"][:12]})' for f in ongoing[:8]],
        "sample_upcoming": [f'{f["name"]} ({f["start"]}~{f["end"]})' for f in out if f not in ongoing][:8],
        "fields": sorted(rows[0].keys()) if rows else [],
    }
    with open(f"{out_dir}/KR.festivals.summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
