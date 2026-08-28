import pytest
from httpx import ASGITransport, AsyncClient

try:
    from geonexa_proxima.api import create_app
    from geonexa_proxima.config import Settings
except ModuleNotFoundError as exc:
    pytest.skip(
        f"API runtime dependency is not installed: {exc.name}",
        allow_module_level=True,
    )


@pytest.mark.asyncio
async def test_health_endpoint_is_independent_from_external_services() -> None:
    application = create_app(settings=Settings(_env_file=None))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
