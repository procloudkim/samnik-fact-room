# Samnik Fact Room

[한국어](README.md) | [English](README.en.md)

Samnik Fact Room keeps OpenDART figures connected to their filing evidence. It combines deterministic financial queries, filing-context retrieval, and explicit evidence states for Samsung Electronics and SK hynix.

> This was a solo project built from July 17 to July 22, 2026. This repository is a portfolio for reviewing the code and engineering decisions.

**Public demo:** ended (unreachable when checked on 2026-10-18)

The homepage and public data API returned HTTP 200 on August 6, 2026. To run it yourself, follow [Local run and verification](#local-run-and-verification).

[![CI](https://github.com/procloudkim/samnik-fact-room/actions/workflows/ci.yml/badge.svg)](https://github.com/procloudkim/samnik-fact-room/actions/workflows/ci.yml)

## Three-minute overview

| Question | Answer |
|---|---|
| Why build it | A financial answer changes with company, period, filing type, consolidated or separate basis, and correction revision. PDF summarization alone does not keep those conditions stable. |
| What was built | PostgreSQL owns structured financial facts, pgvector retrieves official filing context, and an evidence gate validates the response basis. |
| Personal contribution | I completed the scope, data contract, backend, frontend, tests, and deployment checks as a solo project. AI coding tools assisted implementation. |
| Hardest issue | Duplicate account labels across statements and revisions caused some valid values to appear as zero. |
| Final decision | Select the latest valid revision first, then apply metric-specific statement precedence. |
| Verification | Public CI checks regression fixtures, API and frontend tests, and the frontend build. PostgreSQL integration tests require a separate database environment and were not run in the current public release QA. |
| Limits | No user comparison with a general GPT plus the same PDF, continuous operations, large-traffic proof, or performance advantage is claimed. |

Read [project story](docs/PROJECT_STORY.md), [architecture](docs/ARCHITECTURE.ko.md), [AI collaboration](docs/AI_COLLABORATION.md), and [interview guide](docs/INTERVIEW_GUIDE.md) for details.

## Responsibility boundary

This was a solo project with no human teammates. I owned scope, data contracts, backend and frontend implementation, tests, and deployment checks. AI coding tools assisted implementation, while I remained responsible for problem definition, failure conditions, acceptance criteria, and verification.

SQL and deterministic code own financial value selection. An LLM may assist intent and slot classification, but it is not the source of truth for an amount. Missing evidence is not filled with plausible prose.

## Architecture

~~~mermaid
flowchart TD
    U["Question and UI conditions"] --> S["Supervisor"]
    S -->|"Structured finance"| Q["SQL and deterministic calculations"]
    S -->|"Filing context"| R["pgvector retrieval"]
    Q --> P[("PostgreSQL")]
    R --> P
    Q --> G["Evidence Gate"]
    R --> G
    G --> A["Figure, explanation, source location"]
    G -->|"Insufficient or out of scope"| F["Explicit failure state"]
~~~

This is a fixed, evidence-first orchestration flow, not a group of autonomous agents that debate and replan.

## Local run and verification

Requirements are Python 3.12 with uv, Node.js 22+, and Docker Desktop with Compose. Real OpenDART snapshots and credentials are excluded.

The public workflow and its latest status are available in [GitHub Actions CI](https://github.com/procloudkim/samnik-fact-room/actions/workflows/ci.yml).

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

Test counts are not the main claim. Regression coverage for revision selection, metric-specific precedence, evidence states, and failure boundaries is the relevant evidence.

## Current limits

- The public demo responded on August 6, 2026, but continuous availability and ongoing operations have not been verified.
- The public CI does not provision a PostgreSQL service, so six integration tests that require a separate database environment are skipped.
- Real OpenDART snapshots are not redistributed.
- No user study against a general GPT with the same PDF has been run.
- No model A/B test, continuous filing operation, large-traffic proof, high availability, or performance advantage is claimed.
- The exact external runtime model identifier is not verified in public evidence.

## Rights

This is a public portfolio. No open-source license is granted, and visibility does not automatically permit copying, redistribution, or commercial use. OpenDART data and dependencies remain subject to their own terms.

Copyright 2026 ProC. All rights reserved.
