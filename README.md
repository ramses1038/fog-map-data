# fog-map-data

'안개 냠냠' 앱이 내려받는 지도 데이터 (공개).

- `KR.cities.geojson.gz`: 한국 시·군 경계(약 10m 단순화)와 도시별 H3 해상도 11 칸 수
  - 특별시·광역시·특별자치시는 통째로 1개 도시, 도 지역은 시·군 단위
- 데이터는 깃허브 빌드(`.github/workflows/build-kr.yml`)가 만들어 Release에 올린다.

## 출처

지도 데이터 © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/) 라이선스.
원본 자료는 [Geofabrik](https://download.geofabrik.de/)에서 내려받는다.
