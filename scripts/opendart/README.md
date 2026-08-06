# OpenDART snapshot, narrative, and loading pipeline

이 파이프라인은 삼성전자와 SK하이닉스의 구조화 재무제표와 공시 원문을 content-addressed snapshot으로 수집·검증·적재합니다. 모든 Python 명령은 저장소의 Python 3.12 lock 환경을 사용합니다.

```text
OpenDART API
  → raw snapshot
  → narrative preparation
  → enriched snapshot
  → PostgreSQL / pgvector
  → coverage and row-count verification
```

## 지원 범위

| 기간 | 보고서 | OpenDART 코드 | 수치 기준 |
|---|---|---:|---|
| 1분기 | 1분기보고서 | `11013` | 1~3월 누적 |
| 반기 | 반기보고서 | `11012` | 1~6월 누적 |
| 3분기 | 3분기보고서 | `11014` | 1~9월 누적 |
| 연간 | 사업보고서 | `11011` | 연간·4분기 포함 |

지원 회사와 시작연도, report code, CFS/OFS, receipt, 보고기간과 금액 형식은 snapshot 게시 전에 검증합니다.

## 1. 단일 범위 수집

OpenDART API 키는 현재 프로세스 환경에만 둡니다. 명령행 인수, 파일, screenshot과 Git history에 넣지 않습니다.

```powershell
$env:OPENDART_API_KEY = '<issued-key>'

uv run --project backend --frozen python -m scripts.opendart.sync `
  --company all `
  --business-year 2026 `
  --report-code 11013

Remove-Item Env:OPENDART_API_KEY
```

수집 순서는 다음과 같습니다.

1. `list.json`에서 해당 범위의 유효 filing과 최신 정정본을 선택합니다.
2. `fnlttSinglAcntAll.json`에서 CFS/OFS 재무 행을 정규화합니다.
3. `document.xml`에서 narrative용 공시 archive를 받습니다.
4. company, receipt, period, report code, file hash와 manifest ID를 검증합니다.
5. 완성된 snapshot을 `data/snapshots/`에 원자적으로 게시합니다.

정정공시는 기존 snapshot을 수정하지 않고 새로운 snapshot과 filing revision으로 보존합니다. 위험한 ZIP 경로는 해당 문서만 거부하며 구조화 재무 snapshot 전체를 훼손하지 않습니다.

## 2. 최근 3개년·분기 일괄 갱신

`refresh_latest.py`는 회사·연도·보고서 조합을 checkpoint와 실제 HTTP 요청 예산 아래에서 처리합니다.

```powershell
$env:OPENDART_API_KEY = '<issued-key>'

# 먼저 대상과 요청 예산을 확인
uv run --project backend --frozen python -m scripts.opendart.refresh_latest `
  --from-year 2024 `
  --to-year 2026 `
  --company all `
  --dry-run

# 중단 이후 재개
uv run --project backend --frozen python -m scripts.opendart.refresh_latest `
  --from-year 2024 `
  --to-year 2026 `
  --company all `
  --resume `
  --max-requests 720

Remove-Item Env:OPENDART_API_KEY
```

checkpoint의 terminal 상태와 실제 요청 횟수는 삭제하지 않고 다음 실행의 재개 근거로 사용합니다. `VERIFIED`만 건너뛰며 실패 단계는 이미 받은 snapshot이 있으면 API를 다시 호출하지 않고 재개합니다.

## 3. Narrative snapshot 생성

Narrative 준비는 API 자격 증명을 사용하지 않습니다. archive의 모든 지원 XML/HTML을 재검증하고 section path를 보존한 뒤 결정론적 chunk를 만듭니다.

```powershell
uv run --project backend --frozen python -m scripts.opendart.prepare_narrative `
  data/snapshots/<raw-snapshot-id> `
  --local-files-only `
  --device cpu `
  --batch-size 32
```

- 모델: `intfloat/multilingual-e5-small`
- vector: 384차원, finite 검사, L2 정규화
- passage prefix: `passage: `
- 동일 raw snapshot은 중복 embedding하지 않음
- 원본 snapshot은 수정하지 않고 새로운 enriched snapshot을 게시
- 모든 지원 문서 처리가 끝난 경우에만 `FULL_TEXT / N / N` 기록

## 4. PostgreSQL 적재

DB 자격 증명도 현재 환경에만 둡니다.

```powershell
$env:DATABASE_URL = 'postgresql://samnik:<local-password>@127.0.0.1:5432/samnik_fact_room'

uv run --project backend --frozen python -m scripts.opendart.load_snapshot `
  data/snapshots/<enriched-snapshot-id>

Remove-Item Env:DATABASE_URL
```

loader는 다음 불변성을 지킵니다.

- manifest ID와 모든 파일 hash 재검증
- `LIVE_API + OFFICIAL_API_VERIFIED`만 실제 근거로 허용
- snapshot당 하나의 transaction
- append-only snapshot과 filing revision
- stable content/provenance ID
- source JSONL과 DB row count 일치 확인
- no-filing 범위도 CFS/OFS coverage 행으로 설명

## 5. 오프라인 검증

API 키 없이 실행합니다.

```powershell
uv run --project backend --frozen python -m unittest discover -s scripts/opendart/tests -v
```

테스트는 가짜 transport와 embedder를 주입하며 실제 OpenDART, MLAPI 또는 외부 모델을 호출하지 않습니다. 단위 테스트 성공과 자격 증명을 사용한 live 수집 성공은 별도의 증거입니다.

## 데이터·권리 경계

실제 snapshot, archive, PDF, checkpoint와 실행 로그는 Git에서 제외합니다. OpenDART 데이터와 링크는 OpenDART의 이용조건을 따르며, 공개가 파이프라인 코드와 문서의 복제나 재배포 권한을 자동으로 허가하지 않습니다.
