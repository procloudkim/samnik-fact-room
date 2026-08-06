# Synthetic test fixture

이 디렉터리는 결정론적 로컬 테스트 데이터이며 OpenDART API 원본이 아닙니다.

- `evidence_status`는 항상 `SYNTHETIC_UNVERIFIED`입니다.
- receipt와 viewer URL은 테스트가 의도한 공시 범위를 식별할 뿐 공식 API snapshot임을 의미하지 않습니다.
- 일부 기대값은 2026-07-19 DART HTML viewer와 별도로 대조했지만, 이 확인은 fixture의 evidence status를 승격하지 않습니다.
- DS 관련 문장은 retrieval 동작을 위한 합성 문장으로 원문 인용이 아닙니다.
- 실제 `CONFIRMED` 결과에는 `scripts/opendart/sync.py`가 만든 `LIVE_API + OFFICIAL_API_VERIFIED` snapshot이 필요합니다.

manifest hash는 이 fixture payload의 재현성만 보장합니다. OpenDART 원본 응답의 hash로 설명해서는 안 됩니다.

이 fixture는 테스트용 합성 데이터입니다. 공개가 복제나 재배포 권한을 자동으로 허가하지 않으며, OpenDART 원문과 링크는 해당 제공자의 조건을 따릅니다.
