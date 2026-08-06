# 아키텍처와 신뢰 경계

삼전닉스 팩트룸은 언어 모델의 자연스러운 문장과 공식 공시의 사실 판정을 분리합니다. 모델은 질문 분류를 도울 수 있지만 재무 수치를 생성하거나 공시 근거를 대신하지 않습니다.

## 처리 흐름

1. Supervisor가 질문을 실적 조회, 주장 검증, 비교, 투자 조언으로 분류합니다.
2. 회사,기간,지표가 부족하면 조건 확인 단계가 한 번에 한 조건만 확인합니다.
3. 구조화된 재무 수치는 SQL repository가 PostgreSQL에서 조회합니다.
4. 서술형 주장은 RAG 검색 단계가 공식 공시 chunk를 pgvector에서 찾습니다.
5. Evidence Gate가 회사, 보고기간, 연결 기준, 근거 상태를 검증합니다.
6. Supervisor만 공개 응답을 만들며 facts와 evidence를 함께 반환합니다.

코드의 `Agent` 명칭은 독립된 자율 주체가 아니라 위 책임을 분리한 처리 단계입니다. v0.1에는 Agent 간 토론, 동적 계획, 재계획 loop가 없습니다.

## 데이터 신뢰 단계

| 단계 | 역할 | 확정 답변 사용 |
|---|---|---|
| `SYNTHETIC_UNVERIFIED` | 스키마와 로컬 테스트용 fixture | 불가 |
| `OFFICIAL_API_VERIFIED` | OpenDART 응답과 문서 hash 검증 완료 | 가능 |
| `REJECTED` | manifest, hash 또는 의미 검증 실패 | 불가 |

snapshot은 내용 주소 방식의 ID와 파일별 SHA-256을 갖습니다. 이미 적재한 근거 행은 수정,삭제하지 않으며, 정정 공시는 별도의 snapshot으로 추가합니다.

## 판정과 장애의 분리

| 공개 상태 | 의미 |
|---|---|
| `CONFIRMED` | 공식 근거가 답변을 직접 뒷받침함 |
| `CONTRADICTED` | 공식 근거가 입력 주장과 일치하지 않음 |
| `INSUFFICIENT_EVIDENCE` | 조회는 성공했지만 판정할 근거가 부족함 |
| `SYSTEM_UNAVAILABLE` | DB repository 또는 모델 제공자를 사용할 수 없음 |
| `OUT_OF_SCOPE` | 매매 판단,가격 예측 등 지원하지 않는 요청 |

`INSUFFICIENT_EVIDENCE`는 거짓 판정이 아닙니다. `SYSTEM_UNAVAILABLE`도 자료 부재로 바꾸어 표현하지 않습니다.

## 공개 응답의 근거 필드

확정 수치는 같은 적재 데이터에서 다음 필드를 함께 가져옵니다.

- 회사와 지표
- 접수번호와 보고서명
- 보고기간 시작일,종료일
- 연결/별도 기준과 누적/시점 기준
- OpenDART 원수치, 단위 배율, 원 단위 값과 사용자용 환산값
- `source_url`, 공시 섹션, evidence ID

이 구조 덕분에 화면 표현을 바꾸더라도 숫자의 출처와 판정 경계를 추적할 수 있습니다.

## 비밀값과 배포 경계

브라우저는 API 키를 받지 않습니다. OpenDART와 MLAPI 자격 증명은 backend 환경 변수로만 주입합니다. 실제 `.env`, snapshot, PDF, 교육기관 산출물, 개인 작업 기록은 공개 저장소의 일부가 아닙니다.
