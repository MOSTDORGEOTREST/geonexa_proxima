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
    application = create_app(settings=Settings(_env_file=None, admin_password="test-password"))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_not_found_is_404_and_carries_the_reason() -> None:
    """«Не нашлось» не должно выглядеть как поломка сервера.

    Раньше `NotFoundError` из прикладного слоя долетал до пользователя голым
    «Internal Server Error»: админка показывала пятисотку вместо
    «Deployment не зарегистрирован, запусти prefect-worker», и понять причину
    по интерфейсу было нельзя.
    """

    from geonexa_proxima.domain import NotFoundError

    application = create_app(settings=Settings(_env_file=None, admin_password="test-password"))

    @application.get("/_test/missing")
    async def _missing() -> None:
        raise NotFoundError("Deployment «global-harvest» ещё не зарегистрирован")

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/_test/missing")

    assert response.status_code == 404
    assert "global-harvest" in response.json()["detail"]


@pytest.mark.asyncio
async def test_unavailable_orchestrator_is_503() -> None:
    from geonexa_proxima.services.prefect_admin import PrefectUnavailable

    application = create_app(settings=Settings(_env_file=None, admin_password="test-password"))

    @application.get("/_test/prefect")
    async def _down() -> None:
        raise PrefectUnavailable("Prefect недоступен")

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/_test/prefect")

    assert response.status_code == 503
    assert "Prefect" in response.json()["detail"]


@pytest.mark.asyncio
async def test_real_bugs_still_surface_as_500() -> None:
    """Обработчик 404 не должен глотать настоящие ошибки.

    `KeyError` и `IndexError` наследуют `LookupError`. Если бы обработчик
    висел на нём, опечатка в ключе словаря превращалась бы в вежливое
    «не найдено», и баг жил бы в проде незамеченным.
    """

    application = create_app(settings=Settings(_env_file=None, admin_password="test-password"))

    @application.get("/_test/bug")
    async def _bug() -> None:
        {}["отсутствующий ключ"]

    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/bug")

    assert response.status_code == 500
