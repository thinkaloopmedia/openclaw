import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from src.retrieval.fetcher import FetchError, FetchResult, fetch, fetch_many


def _mock_response(status: int, text: str = "<html/>", headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = text
    resp.headers = httpx.Headers(headers or {})
    resp.elapsed = MagicMock()
    resp.elapsed.total_seconds.return_value = 0.05
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_fetch_success():
    with patch("src.retrieval.fetcher.get_client") as mock_get:
        client = AsyncMock()
        client.get.return_value = _mock_response(200, "<html>ok</html>")
        mock_get.return_value = client

        result = await fetch("https://example.com/product/1")

    assert isinstance(result, FetchResult)
    assert result.status_code == 200
    assert result.html == "<html>ok</html>"


@pytest.mark.asyncio
async def test_fetch_retries_on_429_then_succeeds():
    responses = [_mock_response(429), _mock_response(200, "<html>ok</html>")]

    with patch("src.retrieval.fetcher.get_client") as mock_get, \
         patch("src.retrieval.fetcher.asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        client.get.side_effect = responses
        mock_get.return_value = client

        result = await fetch("https://example.com/product/1")

    assert result.status_code == 200
    assert client.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_raises_after_max_retries():
    with patch("src.retrieval.fetcher.get_client") as mock_get, \
         patch("src.retrieval.fetcher.asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        client.get.return_value = _mock_response(503)
        mock_get.return_value = client

        with pytest.raises(FetchError) as exc_info:
            await fetch("https://example.com/product/1")

    assert "max retries exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fetch_raises_on_non_retryable_status():
    resp = _mock_response(404)
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found", request=MagicMock(), response=resp
    )

    with patch("src.retrieval.fetcher.get_client") as mock_get:
        client = AsyncMock()
        client.get.return_value = resp
        mock_get.return_value = client

        with pytest.raises(FetchError) as exc_info:
            await fetch("https://example.com/product/missing")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_fetch_many_returns_errors_without_raising():
    with patch("src.retrieval.fetcher.get_client") as mock_get, \
         patch("src.retrieval.fetcher.asyncio.sleep", new_callable=AsyncMock):
        client = AsyncMock()
        client.get.return_value = _mock_response(503)
        mock_get.return_value = client

        results = await fetch_many(["https://example.com/a", "https://example.com/b"])

    assert all(isinstance(r, FetchError) for r in results)
    assert len(results) == 2


@pytest.mark.asyncio
async def test_fetch_respects_retry_after_header():
    responses = [
        _mock_response(429, headers={"Retry-After": "0.1"}),
        _mock_response(200, "<html>ok</html>"),
    ]

    with patch("src.retrieval.fetcher.get_client") as mock_get, \
         patch("src.retrieval.fetcher.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        client = AsyncMock()
        client.get.side_effect = responses
        mock_get.return_value = client

        await fetch("https://example.com/product/1")

    mock_sleep.assert_called_once_with(0.1)
