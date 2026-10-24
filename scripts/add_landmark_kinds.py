"""이미 만든 KR.landmarks.json.gz 에 아이콘 분류(kind, 13가지)만 붙인다.

명소를 다시 고르는 데는 3시간이 걸리므로(위키데이터 요청 제한), 분류만 바꿀 때는 이것을 쓴다.
사용: python scripts/add_landmark_kinds.py KR.landmarks.json.gz out/
"""
import collections
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from landmark_kinds import KINDS, kind_of  # noqa: E402


def main(src, out_dir):
    with gzip.open(src, "rb") as f:
        data = json.loads(f.read())
    counts = collections.Counter()
    for item in data["landmarks"]:
        item["kind"] = kind_of(item["name"], item.get("wiki", ""), item["cat"])
        counts[item["kind"]] += 1
    os.makedirs(out_dir, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    with gzip.open(f"{out_dir}/KR.landmarks.json.gz", "wb", compresslevel=9) as f:
        f.write(raw)
    print(json.dumps({"landmarks": len(data["landmarks"]),
                      "by_kind": {k: counts.get(k, 0) for k in KINDS}}, ensure_ascii=False))


if __name__ == "__main__":
    main(*sys.argv[1:3])
