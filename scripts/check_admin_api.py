#!/usr/bin/env python3
"""Прогнать admin API против настоящей базы.

Админка собирает запросы строками, а большая их часть живёт внутри функций.
Опечатку в имени колонки не увидит ни линтер, ни тест с заглушкой — она
всплывёт, когда администратор откроет экран. Скрипт поднимает приложение,
логинится и дёргает каждый GET-эндпоинт: любой 500 означает, что SQL разошёлся
со схемой.

Изменяющие эндпоинты проверяются отдельно, на временных записях, и убираются
за собой.

    poetry run python scripts/check_admin_api.py
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import text

from geonexa_proxima.api.application import create_app
from geonexa_proxima.config import get_settings
from geonexa_proxima.db.session import get_engine

OK, BAD, SKIP = "  OK ", " FAIL", "  -- "
failures = 0

#: Эндпоинты, которым нужен внешний сервис, а не база. Их недоступность —
#: не ошибка схемы, поэтому 503 здесь считается допустимым ответом.
EXTERNAL = {
    "/api/admin/prefect/health",
    "/api/admin/prefect/deployments",
    "/api/admin/prefect/flow-runs",
    "/api/admin/prefect/flow-runs/{flow_run_id}/logs",
    "/api/admin/health",
}

#: Параметры для путей с плейсхолдерами. Несуществующий id — законный вход:
#: ожидаем 404, но не 500.
PLACEHOLDERS = {
    "subscriber_id": "00000000-0000-0000-0000-000000000000",
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "profile_id": "00000000-0000-0000-0000-000000000000",
    "job_id": "00000000-0000-0000-0000-000000000000",
    "plan_id": "00000000-0000-0000-0000-000000000000",
    "term_id": "00000000-0000-0000-0000-000000000000",
    "schedule_id": "00000000-0000-0000-0000-000000000000",
    "flow_run_id": "00000000-0000-0000-0000-000000000000",
    "key": "APP_NAME",
    "role": "ranker",
    "provider_key": "deepseek",
}


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"{OK if condition else BAD} {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures += 1


def fill(path: str) -> str | None:
    result = path
    for name, value in PLACEHOLDERS.items():
        result = result.replace("{" + name + "}", value)
    return None if "{" in result else result


async def main() -> int:
    settings = get_settings()
    app = create_app(settings=settings)
    engine = get_engine(settings)

    # raise_app_exceptions=False: нам нужен код ответа, а не трейсбек —
    # иначе первая же ошибка обрывает прогон и остальные остаются ненайденными.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://admin") as client,
        # lifespan не запускаем: он поднимает бота и Prefect, а нам нужна
        # только база. Состояние приложения проставляем вручную.
        _app_state(app, settings, engine),
    ):
        # --- вход ---------------------------------------------------------
        bad = await client.post(
            "/api/admin/auth/login", json={"username": settings.admin_username, "password": "нет"}
        )
        check("неверный пароль отклоняется", bad.status_code == 401, str(bad.status_code))

        response = await client.post(
            "/api/admin/auth/login",
            json={
                "username": settings.admin_username,
                "password": settings.admin_password.get_secret_value(),
            },
        )
        check("вход по паролю из .env", response.status_code == 200, str(response.status_code))
        if response.status_code != 200:
            return 1
        tokens = response.json()
        client.headers["Authorization"] = f"Bearer {tokens['access_token']}"

        anonymous = httpx.AsyncClient(transport=transport, base_url="http://admin")
        guarded = await anonymous.get("/api/admin/dashboard/summary")
        check("без токена внутрь не пускают", guarded.status_code == 401, str(guarded.status_code))
        await anonymous.aclose()

        refreshed = await client.post(
            "/api/admin/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        check("refresh выдаёт новый access", refreshed.status_code == 200)
        access_as_refresh = await client.post(
            "/api/admin/auth/refresh", json={"refresh_token": tokens["access_token"]}
        )
        check(
            "access не принимается вместо refresh",
            access_as_refresh.status_code == 401,
            str(access_as_refresh.status_code),
        )

        # --- все GET-эндпоинты --------------------------------------------
        schema = app.openapi()
        checked = 0
        for path, operations in sorted(schema["paths"].items()):
            if "get" not in operations or not path.startswith("/api/admin"):
                continue
            url = fill(path)
            if url is None:
                continue
            checked += 1
            try:
                result = await client.get(url)
            except Exception as error:
                check(f"GET {path}", False, f"исключение: {str(error).splitlines()[-1][:160]}")
                continue
            if result.status_code >= 500 and path not in EXTERNAL:
                body = result.text[:160].replace("\n", " ")
                check(f"GET {path}", False, f"{result.status_code}: {body}")
            elif (
                result.status_code >= 400
                and result.status_code not in (404, 503)
                and path not in EXTERNAL
            ):
                check(f"GET {path}", False, f"неожиданный {result.status_code}")
        check(f"GET-эндпоинты не падают ({checked} шт.)", True)

        # --- изменяющие операции на временных данных ----------------------
        tag = uuid4().hex[:8]
        chat_id = 960_000_000 + int(tag[:6], 16) % 9_000_000
        created = await client.post(
            "/api/admin/subscribers",
            json={"telegram_chat_id": chat_id, "kind": "user", "title": f"Админ-проба {tag}"},
        )
        check("подписчик создаётся", created.status_code == 201, str(created.status_code))
        subscriber_id = created.json()["id"] if created.status_code == 201 else None

        if subscriber_id:
            duplicate = await client.post(
                "/api/admin/subscribers",
                json={"telegram_chat_id": chat_id, "kind": "user", "title": "дубль"},
            )
            check(
                "повторный chat_id даёт 409",
                duplicate.status_code == 409,
                str(duplicate.status_code),
            )

            wrong_sign = await client.post(
                "/api/admin/subscribers",
                json={"telegram_chat_id": -1, "kind": "user", "title": "знак"},
            )
            check("личный чат с отрицательным id отклоняется", wrong_sign.status_code == 400)

            card = await client.get(f"/api/admin/subscribers/{subscriber_id}")
            check("карточка подписчика читается", card.status_code == 200)

            patched = await client.patch(
                f"/api/admin/subscribers/{subscriber_id}", json={"notes": "проверка"}
            )
            check("правка подписчика применяется", patched.status_code == 200)

            plan = await client.get("/api/admin/plans")
            plan_key = plan.json()[0]["key"] if plan.status_code == 200 and plan.json() else None
            if plan_key:
                granted = await client.post(
                    "/api/admin/subscriptions",
                    json={"subscriber_id": subscriber_id, "plan_key": plan_key, "days": 30},
                )
                check("подписка выдаётся", granted.status_code == 201, str(granted.status_code))
                if granted.status_code == 201:
                    subscription_id = granted.json()["id"]
                    overlap = await client.post(
                        "/api/admin/subscriptions",
                        json={
                            "subscriber_id": subscriber_id,
                            "plan_key": plan_key,
                            "days": 10,
                            "replace_current": False,
                        },
                    )
                    check(
                        "пересечение периодов даёт 409",
                        overlap.status_code == 409,
                        str(overlap.status_code),
                    )
                    extended = await client.post(
                        f"/api/admin/subscriptions/{subscription_id}/extend", json={"days": 10}
                    )
                    check("продление работает", extended.status_code == 200)
                    cancelled = await client.post(
                        f"/api/admin/subscriptions/{subscription_id}/cancel"
                    )
                    check("отмена работает", cancelled.status_code == 200)

            deleted = await client.delete(f"/api/admin/subscribers/{subscriber_id}")
            check("подписчик удаляется", deleted.status_code == 204)

        # --- гейт ----------------------------------------------------------
        probe = await client.post(
            "/api/admin/harvest/test",
            json={
                "title": "Physics-informed neural networks for CPT-based soil classification",
                "abstract": "A PINN fuses cone penetration soundings with a soil model.",
            },
        )
        check("проба гейта отвечает", probe.status_code == 200, str(probe.status_code))
        if probe.status_code == 200:
            body = probe.json()
            check(
                "релевантная статья проходит гейт",
                body["decision"] in {"accepted", "borderline"},
                f"{body['decision']}, score={body['keyword_score']}",
            )
        noise = await client.post(
            "/api/admin/harvest/test",
            json={"title": "Deep learning for protein folding prediction", "abstract": ""},
        )
        if noise.status_code == 200:
            check(
                "нерелевантная отклоняется",
                noise.json()["decision"] == "rejected",
                noise.json()["decision"],
            )

        # --- cron ----------------------------------------------------------
        cron = await client.post("/api/admin/schedules/validate", json={"cron": "0 7 * * 1"})
        check("разбор cron работает", cron.status_code == 200 and cron.json().get("valid") is True)
        broken = await client.post("/api/admin/schedules/validate", json={"cron": "не крон"})
        check("битый cron отклоняется", broken.json().get("valid") is False)

        # --- настройки -----------------------------------------------------
        env_only = await client.put("/api/admin/settings/DATABASE_URL", json={"value": "нет"})
        check(
            "env-only настройку менять нельзя",
            env_only.status_code in (400, 404),
            str(env_only.status_code),
        )

        # --- аудит записался ------------------------------------------------
        async with engine.connect() as connection:
            audited = await connection.scalar(
                text(
                    "SELECT count(*) FROM admin_audit_log WHERE created_at > now()"
                    " - interval '5 minutes'"
                )
            )
        check("действия пишутся в аудит", int(audited or 0) > 0, f"{audited} записей")

    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM subscribers WHERE title LIKE 'Админ-проба %'"))
    from geonexa_proxima.db.session import dispose_engines

    await dispose_engines()

    print()
    print("Всё сошлось" if not failures else f"Провалов: {failures}")
    return 1 if failures else 0


class _app_state:
    """Проставить приложению зависимости без запуска lifespan."""

    def __init__(self, app: Any, settings: Any, engine: Any) -> None:
        self.app = app
        self.settings = settings
        self.engine = engine

    async def __aenter__(self) -> None:
        from geonexa_proxima.db.session import create_session_factory
        from geonexa_proxima.services.container import Container

        self.app.state.container = Container(
            settings=self.settings,
            engine=self.engine,
            session_factory=create_session_factory(self.engine),
        )
        self.app.state.bootstrap = None
        self.app.state.startup_error = None

    async def __aexit__(self, *exc: object) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
