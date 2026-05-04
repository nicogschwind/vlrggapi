import pytest
from httpx import ASGITransport, AsyncClient

from main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def async_client():
    transport = ASGITransport(app=app)
    # We add a default X-API-Key header to all test requests
    # unless a specific test needs to test unauthorized access.
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": "test_key"}
    ) as client:
        yield client
