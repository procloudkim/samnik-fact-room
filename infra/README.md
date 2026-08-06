# Local PostgreSQL and pgvector

이 Compose stack은 개발·검증용 PostgreSQL과 pgvector를 `127.0.0.1`에만 바인딩합니다.

```powershell
$env:POSTGRES_PASSWORD = '<local-development-password>'
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps
```

최초 초기화 시 `infra/migrations`가 순서대로 적용되고, 명확히 표시된 synthetic fixture가 로드됩니다. fixture의 모든 evidence는 `SYNTHETIC_UNVERIFIED`이므로 실제 서비스의 `CONFIRMED` 근거가 될 수 없습니다.

## 데이터 불변성

- snapshot과 filing revision은 append-only입니다.
- 정정공시는 기존 근거를 수정하지 않고 새 content-addressed snapshot으로 적재합니다.
- loader는 snapshot당 하나의 transaction을 사용합니다.
- `narrative_corpus_status`는 본문 미생성, 부분 적재와 `FULL_TEXT`를 구분합니다.
- 공시 부재 판정은 coverage가 완전할 때만 허용합니다.

## 재초기화 주의

routine startup에서 기존 volume을 삭제하지 않습니다. 완전한 재초기화가 필요하면 보존할 데이터가 없음을 먼저 확인한 뒤 새 Compose project를 사용하거나 명시적으로 개발 volume을 정리합니다.

배포 환경의 PostgreSQL과 FastAPI는 이 로컬 Compose 안내와 별도이며, 외부 포트 노출과 운영 자격 증명을 저장소에 기록하지 않습니다.
