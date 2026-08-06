from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .config import RuntimeMode, Settings
from .domain import ChatTurnRequest, Company, EventType, FinalAnswer, TurnResult
from .errors import RepositoryUnavailable
from .financial_queries import FinancialQueryRequest, PostgresFinancialQueryRepository
from .public_contract import PublicEvent, to_public_events, to_public_progress_event
from .runtime import build_external_workflow, build_local_workflow
from .timeline import build_financial_coverage_timeline
from .workflow import FactRoomWorkflow


def _normalize_csv_params(values: list[str] | None) -> list[str]:
    if not values:
        return []
    parsed: list[str] = []
    for item in values:
        for part in str(item).split(","):
            value = part.strip()
            if value:
                parsed.append(value)
    return parsed


def create_app(
    workflow: FactRoomWorkflow | None = None,
    *,
    settings: Settings | None = None,
    financial_repository: PostgresFinancialQueryRepository | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if workflow is not None:
            app.state.workflow = workflow
            app.state.financial_query_repository = financial_repository or getattr(
                workflow, "financial_query_repository", None
            )
            yield
            return
        if runtime_settings.runtime_mode == RuntimeMode.LOCAL:
            app.state.workflow = build_local_workflow()
            app.state.financial_query_repository = financial_repository
            yield
            return
        async with build_external_workflow(runtime_settings) as external_workflow:
            app.state.workflow = external_workflow
            app.state.financial_query_repository = getattr(
                external_workflow, "financial_query_repository", None
            )
            yield

    app = FastAPI(
        title="Samnik Fact Room API",
        version="0.1.0",
        lifespan=lifespan,
    )
    if financial_repository is not None:
        app.state.financial_query_repository = financial_repository
    if workflow is not None:
        app.state.workflow = workflow
        app.state.financial_query_repository = financial_repository or getattr(
            workflow, "financial_query_repository", None
        )

    def financials(request: Request) -> PostgresFinancialQueryRepository:
        repository = getattr(request.app.state, "financial_query_repository", None)
        if repository is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "status": "SYSTEM_UNAVAILABLE",
                    "reason": "REPOSITORY_UNAVAILABLE",
                },
            )
        return repository

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/chat/turns/stream")
    async def stream_chat_turn(payload: ChatTurnRequest, request: Request) -> StreamingResponse:
        async def generate():
            active_workflow: FactRoomWorkflow = request.app.state.workflow
            active_payload = payload
            if active_payload.conversation_id is None:
                active_payload = active_payload.model_copy(
                    update={"conversation_id": str(uuid4())}
                )
            public_seq = 0
            started = PublicEvent(
                request_id=active_payload.client_request_id,
                conversation_id=active_payload.conversation_id,
                seq=public_seq,
                type="run.started",
                data={"message": active_payload.message},
            )
            yield started.model_dump_json() + "\n"
            await asyncio.sleep(0)

            internal_events = []
            seen_stages: set[str] = set()
            async for internal_event in active_workflow.stream_turn(active_payload):
                internal_events.append(internal_event)
                if internal_event.event_type == EventType.TURN_STARTED:
                    continue
                progress = to_public_progress_event(
                    active_payload,
                    internal_event,
                    seq=public_seq + 1,
                )
                if progress is None or progress.data["stage"] in seen_stages:
                    continue
                seen_stages.add(progress.data["stage"])
                public_seq += 1
                yield progress.model_dump_json() + "\n"
                await asyncio.sleep(0)

            answer_event = next(
                (
                    event
                    for event in reversed(internal_events)
                    if event.event_type == EventType.ANSWER_COMPLETED
                ),
                None,
            )
            if answer_event is None:
                return
            result = TurnResult(
                events=internal_events,
                answer=FinalAnswer.model_validate(answer_event.payload["answer"]),
            )
            terminal = to_public_events(active_payload, result)[-1].model_copy(
                update={"seq": public_seq + 1}
            )
            yield terminal.model_dump_json() + "\n"

        return StreamingResponse(
            generate(),
            media_type="application/jsonl",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/financials/query")
    async def query_financials(payload: FinancialQueryRequest, request: Request) -> dict[str, Any]:
        try:
            return await financials(request).query(payload)
        except RepositoryUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"status": "SYSTEM_UNAVAILABLE", "reason": "REPOSITORY_UNAVAILABLE"},
            ) from None

    @app.get("/api/v1/financials/accounts")
    async def search_financial_accounts(
        request: Request,
        q: str = Query(min_length=1, max_length=200),
        company: Company | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        as_of: datetime = Query(default_factory=lambda: datetime.now(UTC)),
    ) -> dict[str, Any]:
        try:
            accounts = await financials(request).search_accounts(
                company=company, query=q, limit=limit, as_of=as_of
            )
            return {"accounts": accounts, "ambiguous": len(accounts) > 1}
        except RepositoryUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"status": "SYSTEM_UNAVAILABLE", "reason": "REPOSITORY_UNAVAILABLE"},
            ) from None

    @app.get("/api/v1/financials/coverage")
    async def get_financial_coverage(
        request: Request,
        company: Company | None = None,
        start_year: int = Query(default=2015, ge=2015, le=2100),
        end_year: int = Query(default=2100, ge=2015, le=2100),
        report_codes: list[str] | None = Query(default=None),
        fs_divs: list[str] | None = Query(default=None),
    ) -> dict[str, Any]:
        if start_year > end_year:
            raise HTTPException(status_code=422, detail="start_year must not exceed end_year")
        report_code_filters = _normalize_csv_params(report_codes)
        fs_div_filters = _normalize_csv_params(fs_divs)
        try:
            return {
                "coverage": await financials(request).get_coverage(
                    companies=[company] if company is not None else None,
                    start_year=start_year,
                    end_year=end_year,
                    report_codes=report_code_filters or None,
                    fs_divs=fs_div_filters or None,
                )
            }
        except RepositoryUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"status": "SYSTEM_UNAVAILABLE", "reason": "REPOSITORY_UNAVAILABLE"},
            ) from None

    @app.get("/api/v1/financials/timeline")
    async def get_financial_timeline(
        request: Request,
        companies: list[Company] | None = Query(default=None),
        completed_years: int = Query(default=10, ge=3, le=20),
        include_current: bool = Query(default=True),
        as_of: datetime = Query(default_factory=lambda: datetime.now(UTC)),
    ) -> dict[str, Any]:
        if not include_current:
            raise HTTPException(
                status_code=422,
                detail="the current filing year is required by the timeline contract",
            )
        del completed_years  # all locally supported years remain visible
        try:
            timeline = await build_financial_coverage_timeline(
                financials(request),
                as_of=as_of,
                companies=companies,
            )
            return timeline.model_dump(mode="json")
        except RepositoryUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"status": "SYSTEM_UNAVAILABLE", "reason": "REPOSITORY_UNAVAILABLE"},
            ) from None

    return app
