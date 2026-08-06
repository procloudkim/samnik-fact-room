# 기여 안내

이 저장소는 포트폴리오 검토를 위해 공개하는 개인 프로젝트입니다. 현재 외부 기여를 적극 모집하지는 않지만, 재현 가능한 오류 보고와 작은 개선 제안은 검토할 수 있어요.

## 변경 전 확인

1. 실제 API 키, .env, 개인 연락처, PDF, 실제 snapshot, 내부 메모가 없는지 확인합니다.
2. 테스트에는 실제 공시처럼 보이지 않는 synthetic fixture를 사용합니다.
3. CONFIRMED, INSUFFICIENT_EVIDENCE, SYSTEM_UNAVAILABLE의 경계를 보존합니다.
4. 아래 검증을 실행해요.

~~~powershell
uv run --project backend --frozen pytest backend
uv run --project backend --frozen python -m unittest discover -s scripts/opendart/tests -v

Set-Location frontend
npm test
npm run typecheck
npm run build
Set-Location ..

pwsh scripts/release/check_public_tree.ps1 -Mode Candidate
~~~

변경 제안에는 목적, 확인한 명령과 결과, 외부 서비스를 실제로 호출했는지 여부를 적습니다. 비밀값이나 개인정보가 들어간 로그는 첨부하지 않아요.
