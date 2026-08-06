# Samnik Fact Room backend

Python 3.12와 FastAPI로 구현한 근거 중심 금융 질의 워크플로입니다.

```text
Supervisor
  ├─ 조건 부족 → Scenario → Supervisor
  └─ 조건 충족 → RAG/Financial Diagnosis
                    ├─ Context Analysis
                    └─ Financial Literacy Tutor
                          ↓
                    Evidence Gate
                          ↓
                    Supervisor response
```

## 책임 경계

- `workflow.py`: 고정 LangGraph 흐름, 라우팅과 최종 응답 상태
- `agents.py`: 재무 계산, 공시 검색, 문맥 분류, 쉬운 설명, 근거 검증
- `postgres.py`: 공식 snapshot과 revision을 대상으로 한 SQL/pgvector 조회
- `public_contract.py`: 내부 이벤트를 브라우저용 JSONL 계약으로 제한
- `providers.py`: 선택적 MLAPI 분류기와 embedding adapter

LLM은 필요할 때 의도와 슬롯 분류를 돕지만 재무 금액의 진실 원천이 아닙니다. 금액·비율·비교는 SQL과 결정론 코드가 소유하며, 확정 응답은 `OFFICIAL_API_VERIFIED` 근거를 통과해야 합니다.

## HTTP 경계

`POST /api/v1/chat/turns/stream`은 `application/jsonl` 형식으로 진행 이벤트와 최종 결과를 반환합니다. 브라우저는 내부 예외, 자격 증명 또는 원시 repository payload를 받지 않습니다.

## 로컬 실행

저장소 루트에서 실행합니다.

```powershell
Copy-Item .env.example .env
uv sync --project backend --frozen --dev
uv run --project backend --frozen uvicorn samnik_backend.main:app --host 127.0.0.1 --reload
```

루트 `.env.example`이 환경 변수 계약의 기준입니다. 기본 `SAMNIK_RUNTIME_MODE=local`은 synthetic fixture와 로컬 provider를 사용합니다. 실제 PostgreSQL과 외부 MLAPI를 사용할 때만 해당 repository와 provider를 주입합니다.

`CHECKPOINTER_MODE=none`이 Windows 호환 기본값입니다. 멀티턴 슬롯은 PostgreSQL conversation 저장소가 담당하고, 턴 단위 graph checkpoint는 프로세스에 남기지 않습니다.

## 데이터와 embedding

DB schema의 단일 소유자는 `../infra/migrations`입니다. backend는 별도 migration tree를 만들지 않습니다.

Narrative retrieval은 CPU에서 `intfloat/multilingual-e5-small`의 384차원 L2 정규화 vector를 사용합니다. 로딩은 지연되므로 SQL 전용 요청은 embedding 모델을 초기화하지 않습니다.

## 검증

```powershell
uv run --project backend --frozen pytest backend
```

일반 단위 테스트는 실제 네트워크를 차단합니다. live PostgreSQL integration은 명시적인 DB 환경이 있을 때 별도 실행되며, 단위 테스트 성공을 live 데이터 증거로 간주하지 않습니다.
