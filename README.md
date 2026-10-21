# 삼전닉스 팩트룸

[한국어](README.md) | [English](README.en.md)

OpenDART 공시의 숫자와 원문 근거를 함께 확인하도록 만든 금융 리서치 도구입니다. 삼성전자와 SK하이닉스를 대상으로 구조화 재무 조회, 공시 문맥 검색, 근거 상태 판정을 하나의 흐름으로 연결했습니다.

> 2026년 7월 17일부터 22일까지 진행한 개인 프로젝트입니다. 이 저장소는 코드와 설계 판단을 검토하기 위한 포트폴리오입니다.

**공개 데모:** 종료(2026-10-18 접속 불가 확인)

2026년 8월 6일에는 홈페이지와 공개 데이터 API가 HTTP 200으로 응답했습니다. 지금은 [로컬 실행과 검증](#로컬-실행과-검증)의 절차로 직접 실행할 수 있습니다.

[![CI](https://github.com/procloudkim/samnik-fact-room/actions/workflows/ci.yml/badge.svg)](https://github.com/procloudkim/samnik-fact-room/actions/workflows/ci.yml)

![삼전닉스 팩트룸 워크플로](docs/samnik-fact-room-workflow-wireframe.svg)

## 3분 요약

| 질문 | 답변 |
|---|---|
| 왜 시작했나 | 재무 질문은 회사, 기간, 보고서, 연결 또는 별도 기준, 정정공시 여부가 맞아야 같은 숫자를 재현할 수 있습니다. PDF 요약만으로는 이 조건을 지속적으로 통제하기 어렵다고 판단했습니다. |
| 무엇을 만들었나 | PostgreSQL의 구조화 재무 조회와 pgvector의 공시 문맥 검색을 결합하고, 답변마다 근거 상태와 원문 위치를 연결했습니다. |
| 내 역할은 무엇인가 | 주제와 범위 설정, data contract, 백엔드와 프론트엔드 구현, 테스트, 배포 확인까지 혼자 수행했습니다. AI 코딩 도구는 구현 보조로 사용했습니다. |
| 가장 어려웠던 문제는 무엇인가 | 같은 계정명이 여러 재무제표와 정정본에 있을 때 잘못된 행을 선택해 일부 값이 0으로 보이는 문제였습니다. |
| 어떻게 해결했나 | 최신 유효 revision을 먼저 선택하고, 지표마다 사용할 재무제표의 우선순위를 따로 정했습니다. |
| 어떻게 확인했나 | 공개 CI에서 synthetic fixture 기반 회귀 테스트, API와 프론트엔드 테스트 및 build를 확인했습니다. PostgreSQL 통합 테스트는 별도 DB 환경이 필요하며 현재 공개 릴리스 QA에서는 실행하지 못했습니다. |
| 아직 무엇이 부족한가 | 범용 GPT에 같은 PDF를 준 경우와의 사용자 비교, 지속 운영, 대규모 트래픽, 성능 우위는 입증하지 않았습니다. |

추천 읽기 순서는 [프로젝트 판단 기록](docs/PROJECT_STORY.md), [아키텍처와 신뢰 경계](docs/ARCHITECTURE.ko.md), [AI 협업 경계](docs/AI_COLLABORATION.md), [면접 설명 가이드](docs/INTERVIEW_GUIDE.md)입니다.

## 문제 정의

“2025년 삼성전자 연결 영업이익은?”이라는 질문도 연도, 보고서, CFS 또는 OFS, 누적기간, 정정공시 revision에 따라 답이 달라집니다. 이 프로젝트는 답변 문장을 늘리는 것보다 위 조건을 코드와 data contract로 고정하는 데 초점을 맞췄습니다. 숫자는 SQL과 deterministic 코드가 계산하고, LLM은 필요한 경우 질문 의도와 조건 분류를 돕습니다.

## 역할과 기여 범위

사람 팀원이 없는 개인 프로젝트이며 다음 범위를 직접 책임졌습니다.

- 두 기업과 보고서, 표준 지표의 지원 범위 결정
- OpenDART 수집, snapshot, manifest, 적재 흐름 설계
- PostgreSQL과 pgvector 기반 데이터 모델과 조회 계층 구현
- 근거 상태와 실패 상태의 public contract 정의
- FastAPI 백엔드, React 프론트엔드, 테스트와 배포 확인
- 오류 재현, 선택 규칙 수정, 회귀 테스트와 공개 범위 검증

AI 코딩 도구는 코드 초안과 검토에 사용했습니다. 문제와 범위, 실패 조건, acceptance criteria, 최종 검증은 제가 책임졌습니다. 프로젝트별 prompt와 제안 거절 이력은 정리된 증거가 없어 공개 주장에는 넣지 않습니다.

## 가장 어려웠던 문제

### 증상과 원인

2024년과 2025년의 일부 값이 0으로 표시됐지만 원문에는 값이 있었습니다. 같은 계정명이 여러 재무제표에 있고 정정공시 revision도 남을 수 있는데, 계정명만으로 행을 선택한 것이 원인이었습니다.

### 최종 선택

1. 요청 시점의 최신 유효 revision을 먼저 고릅니다.
2. 매출과 영업이익은 IS와 CIS, 자산과 부채는 BS, 현금흐름은 CF처럼 지표별 재무제표 우선순위를 적용합니다.
3. 회사, 기간, 보고서, CFS 또는 OFS 조건이 맞지 않으면 확정 답변을 만들지 않습니다.

기존 동작과 최종 규칙은 코드와 테스트로 확인할 수 있습니다. 당시 검토한 다른 대안은 상세 기록이 남아 있지 않아 추측하지 않습니다.

### 결과 확인

- 중복 행과 정정 revision이 있는 입력을 회귀 fixture로 고정
- PostgreSQL 통합 테스트 코드에 대표값 선택과 state contract를 고정하되, 현재 공개 릴리스 QA에서는 해당 경로를 실행하지 못했으므로 통과 성과로 사용하지 않음
- API 응답의 회사, 기간, CFS 또는 OFS, 근거 상태 확인
- 근거 부족과 시스템 장애가 CONFIRMED로 바뀌지 않는지 확인

이 해결은 데이터 선택의 정확성을 다룬 것이며, 성능 최적화나 대규모 트래픽 성과로 표현하지 않습니다.

## 아키텍처

~~~mermaid
flowchart TD
    U["사용자 질문과 화면 조건"] --> S["Supervisor"]
    S -->|"구조화 재무 질문"| Q["SQL 조회와 deterministic 계산"]
    S -->|"공시 문맥 질문"| R["pgvector 근거 검색"]
    Q --> P[("PostgreSQL")]
    R --> P
    Q --> G["Evidence Gate"]
    R --> G
    G --> A["숫자, 쉬운 설명, 원문 위치"]
    G -->|"근거 부족 또는 범위 밖"| F["명시적 실패 상태"]
~~~

v0.1은 LangGraph의 고정된 흐름 안에서 재무 계산, 공시 검색, 설명, 근거 검증의 책임을 나눈 오케스트레이션입니다. 여러 자율 agent가 토론하고 다시 계획을 세우는 시스템은 아닙니다.

| 구성 요소 | 책임 |
|---|---|
| OpenDART pipeline | 원천 수집, content hash, manifest, revision 보존 |
| PostgreSQL | 구조화 재무 사실과 공시 메타데이터의 source of truth |
| pgvector | 공식 공시 원문 chunk 검색 |
| Deterministic 계산 | 금액, 비율, 전년 대비, 동일 기준 비교 |
| Evidence Gate | 회사, 기간, 보고서, CFS 또는 OFS, 근거 상태 검증 |
| LLM 보조 | 필요한 경우 의도와 slot 분류, 쉬운 설명 보조 |

## 로컬 실행과 검증

요구 환경은 Python 3.12와 uv, Node.js 22 이상, Docker Desktop과 Compose입니다. 실제 OpenDART snapshot과 자격 증명은 포함하지 않습니다.

현재 공개 workflow와 최신 실행 상태는 [GitHub Actions CI](https://github.com/procloudkim/samnik-fact-room/actions/workflows/ci.yml)에서 확인할 수 있습니다.

~~~powershell
Copy-Item .env.example .env
docker compose -f infra/docker-compose.yml up -d

uv sync --project backend --frozen --dev
uv run --project backend --frozen uvicorn samnik_backend.main:app --host 127.0.0.1 --reload

Set-Location frontend
npm ci
npm run dev
~~~

~~~powershell
uv run --project backend --frozen pytest backend
uv run --project backend --frozen python -m unittest discover -s scripts/opendart/tests -v

Set-Location frontend
npm test
npm run typecheck
npm run build
~~~

테스트 개수는 핵심 성과로 제시하지 않습니다. 대표값 선택, revision 보존, 근거 상태, 실패 경계가 회귀 테스트로 남아 있다는 점이 중요합니다.

## 현재 한계

- 공개 데모는 2026년 8월 6일 기준 응답을 확인했지만, 상시 가용성이나 지속 운영을 검증하지 않았습니다.
- 현재 공개 CI는 PostgreSQL service를 구성하지 않으므로 별도 DB 환경이 필요한 통합 테스트 여섯 건은 건너뜁니다.
- 실제 OpenDART snapshot은 재배포하지 않습니다.
- 범용 GPT에 같은 PDF를 제공한 경우와 사용자 이해도를 비교하지 않았습니다.
- 모델 A/B 테스트, 연속 공시 운영, 대규모 트래픽, 고가용성, 성능 우위를 입증하지 않았습니다.
- 외부 모델 공급자의 정확한 런타임 모델 ID는 공개 증거로 확인하지 않았습니다.

## 공개와 이용 범위

이 저장소는 포트폴리오 검토를 위한 공개본입니다. 오픈소스 라이선스를 부여하지 않으며, 공개했다고 해서 복제, 재배포, 상업적 이용을 허가하는 것은 아닙니다. OpenDART 데이터와 각 의존성은 해당 제공자의 조건을 따릅니다.

Copyright 2026 ProC. All rights reserved.
