import httpx
import pytest

from samnik_backend.errors import ProviderUnavailable
from samnik_backend.providers import OpenAICompatibleEmbeddingProvider, OpenDartClient


@pytest.mark.asyncio
async def test_opendart_transport_failure_does_not_retain_key_bearing_cause() -> None:
    secret = "synthetic-secret-that-must-not-appear"

    def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="synthetic upstream failure")

    transport = httpx.MockTransport(fail)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenDartClient(secret, client=client)
        with pytest.raises(ProviderUnavailable) as caught:
            await provider.list_filings("00126380", "20260101", "20260630")

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_embedding_http_failure_does_not_retain_bearer_header_cause() -> None:
    secret = "synthetic-embedding-secret"

    def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="synthetic upstream failure")

    transport = httpx.MockTransport(fail)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAICompatibleEmbeddingProvider(
            base_url="https://embedding.invalid",
            api_key=secret,
            model="synthetic-model",
            client=client,
        )
        with pytest.raises(ProviderUnavailable) as caught:
            await provider.embed("synthetic input")

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
