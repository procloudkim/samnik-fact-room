from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from .agents import EvidenceGate, MultiTurnScenarioAgent, RagFaqAgent
from .config import CheckpointerMode, Settings
from .postgres import (
    PostgresConversationRepository,
    PostgresEvidenceRepository,
    PostgresFinancialRepository,
    postgres_checkpointer,
)
from .financial_queries import PostgresFinancialQueryRepository
from .providers import (
    EliceMLAPIProvider,
    HashEmbeddingProvider,
    LocalSentenceTransformerEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    RuleBasedLanguageModel,
)
from .repositories import (
    InMemoryConversationRepository,
    InMemoryEvidenceRepository,
    InMemoryFinancialRepository,
)
from .workflow import FactRoomWorkflow


def build_local_workflow() -> FactRoomWorkflow:
    """Boot-safe local runtime with no external calls and no pretend evidence."""

    evidence = InMemoryEvidenceRepository(
        corpus_complete=False,
        corpus_scope="empty local runtime",
    )
    return FactRoomWorkflow(
        language_model=RuleBasedLanguageModel(),
        conversations=InMemoryConversationRepository(),
        scenario_agent=MultiTurnScenarioAgent(),
        rag_agent=RagFaqAgent(
            financials=InMemoryFinancialRepository(),
            evidence=evidence,
            embeddings=HashEmbeddingProvider(),
        ),
        evidence_gate=EvidenceGate(),
        checkpointer=None,
    )


@asynccontextmanager
async def build_external_workflow(settings: Settings) -> AsyncIterator[FactRoomWorkflow]:
    missing = [
        name
        for name, value in {
            "DATABASE_URL": settings.postgres_dsn,
            "ELICE_MLAPI_API_KEY": settings.elice_api_key,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"external runtime is missing required settings: {', '.join(missing)}")

    assert settings.postgres_dsn is not None
    assert settings.elice_api_key is not None

    pool = await asyncpg.create_pool(settings.postgres_dsn, min_size=1, max_size=5)
    language_model = EliceMLAPIProvider(
        base_url=settings.elice_base_url,
        api_key=settings.elice_api_key,
        model=settings.elice_model,
        chat_path=settings.elice_chat_path,
        timeout_seconds=settings.request_timeout_seconds,
    )
    if settings.embedding_base_url and settings.embedding_api_key and settings.embedding_model:
        embeddings = OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            path=settings.embedding_path,
            timeout_seconds=settings.request_timeout_seconds,
        )
    else:
        embeddings = LocalSentenceTransformerEmbeddingProvider(
            model_name=settings.embedding_model,
        )
    evidence = PostgresEvidenceRepository(
        pool,
        embedding_model=settings.embedding_model,
    )

    def make_workflow(checkpointer) -> FactRoomWorkflow:
        workflow = FactRoomWorkflow(
                language_model=language_model,
                conversations=PostgresConversationRepository(pool),
                scenario_agent=MultiTurnScenarioAgent(),
                rag_agent=RagFaqAgent(
                    financials=PostgresFinancialRepository(pool),
                    evidence=evidence,
                    embeddings=embeddings,
                    minimum_score=settings.evidence_min_score,
                    top_k=settings.evidence_top_k,
                ),
                evidence_gate=EvidenceGate(),
                checkpointer=checkpointer,
            )
        workflow.financial_query_repository = PostgresFinancialQueryRepository(pool)
        return workflow

    try:
        if settings.checkpointer_mode == CheckpointerMode.POSTGRES:
            async with postgres_checkpointer(settings.postgres_dsn) as checkpointer:
                yield make_workflow(checkpointer)
        else:
            # Conversation slots remain durable in PostgreSQL. A per-turn graph
            # needs no retained checkpoint state; disabling it also prevents
            # unauthenticated request IDs from growing process memory.
            yield make_workflow(None)
    finally:
        await language_model.aclose()
        close = getattr(embeddings, "aclose", None)
        if close is not None:
            await close()
        await pool.close()
