"""Подписчики, их профили и интересы."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from geonexa_proxima.api.admin.deps import (
    Admin,
    AppContainer,
    AppSettings,
    Engine,
    Paging,
    Profiles,
    Subscribers,
    audit,
    conflict_from,
    execute,
    fetch_all,
    fetch_one,
    page_response,
    require,
    returning,
)
from geonexa_proxima.db.subscriber_repository import (
    SubscriberNotFoundError,
    kind_from_chat_type,
)
from geonexa_proxima.domain import (
    ALL_KINDS,
    InterestPolarity,
    SubscriberKind,
    UserStatus,
)
from geonexa_proxima.services.facets import DESCRIPTION_SECTION, INTERESTS_SECTION
from geonexa_proxima.services.profile_guide import as_payload, preview
from geonexa_proxima.telegram.onboarding import approval_message

router = APIRouter(tags=["admin:subscribers"])


class SubscriberPatch(BaseModel):
    status: UserStatus | None = None
    notes: str | None = None
    timezone: str | None = None
    is_owner: bool | None = None
    title: str | None = None


class SubscriberCreate(BaseModel):
    telegram_chat_id: int
    kind: SubscriberKind = SubscriberKind.USER
    title: str | None = None
    chat_type: str | None = None
    notes: str | None = None


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ApproveRequest(BaseModel):
    """Что сделать в момент подтверждения, кроме смены статуса."""

    notify: bool = True
    grant_trial: bool = True
    description: str | None = Field(default=None, max_length=4000)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    is_active: bool = True
    digest_enabled: bool = False


class InterestCreate(BaseModel):
    #: Оба написания через «;»: «liquefaction; разжижение грунтов». Буквальная
    #: сверка проверяет каждое, вектор темы берёт строку целиком.
    query: str = Field(min_length=2, max_length=500)
    polarity: InterestPolarity = InterestPolarity.POSITIVE
    weight: float = Field(default=5, ge=0, le=10)


class PreviewRequest(BaseModel):
    """Черновик описания — до сохранения, прямо из поля ввода."""

    description: str = Field(default="", max_length=8000)
    #: Чей профиль правим. Нужен, чтобы к черновику описания добавились уже
    #: сохранённые явные темы: без них список тем в предпросмотре короче того,
    #: по которому реально пойдёт поиск, и человек правит вслепую.
    profile_id: UUID | None = None


@router.get("/subscribers")
async def list_subscribers(
    admin: Admin,
    repository: Subscribers,
    paging: Paging,
    kind: Annotated[list[str] | None, Query()] = None,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
    q: str | None = None,
    has_active_subscription: bool | None = None,
) -> dict[str, Any]:
    """Список подписчиков нужных видов с постраничностью."""

    kwargs: dict[str, Any] = {
        "kinds": kind or None,
        "statuses": status_filter or None,
        "search": q,
        "with_active_subscription": has_active_subscription,
    }
    try:
        rows = await repository.list_subscribers(**kwargs, limit=paging.limit, offset=paging.offset)
        total = await repository.count_subscribers(**kwargs)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return page_response([r.model_dump() for r in rows], total, paging)


@router.get("/subscribers/breakdown")
async def breakdown(admin: Admin, repository: Subscribers) -> dict[str, Any]:
    """Разрез «вид × статус» для верхней строки дашборда."""

    rows = await repository.breakdown()
    return {
        "kinds": ALL_KINDS,
        "rows": [{"kind": r.kind, "status": r.status, "count": r.count} for r in rows],
    }


@router.get("/subscribers/pending")
async def pending(admin: Admin, repository: Subscribers) -> dict[str, Any]:
    """Заявки, ждущие решения: люди и чаты одним списком.

    Отдельный адрес, а не фильтр по списку подписчиков: в очереди нужны поля
    членства бота — сколько участников, есть ли право постить, кто добавил, —
    и без них решать «пускать или нет» приходится вслепую.
    """

    limit = 100
    rows = await repository.pending_queue(limit=limit)
    # Обрезанный список нельзя показывать как полный: счётчик в шапке замер бы
    # на сотне, а заявки за сотой не увидел бы никто.
    return {"items": rows, "total": len(rows), "truncated": len(rows) >= limit}


@router.post("/subscribers", status_code=status.HTTP_201_CREATED)
async def create_subscriber(
    payload: SubscriberCreate, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    """Завести подписчика вручную — например, канал, куда бота ещё не добавили."""

    kind = payload.kind.value
    if payload.chat_type:
        kind = kind_from_chat_type(payload.chat_type)
    if kind == SubscriberKind.USER.value and payload.telegram_chat_id <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="chat_id личного чата положителен")
    if kind != SubscriberKind.USER.value and payload.telegram_chat_id >= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="chat_id группы или канала отрицателен"
        )
    try:
        row = await returning(
            db,
            text(
                "INSERT INTO subscribers (id, kind, telegram_chat_id, title, notes, status)"
                " VALUES (gen_random_uuid(), :kind, :chat_id, :title, :notes, 'active')"
                " RETURNING id, kind, telegram_chat_id, title, status"
            ),
            {
                "kind": kind,
                "chat_id": payload.telegram_chat_id,
                "title": payload.title,
                "notes": payload.notes,
            },
        )
    except Exception as error:
        from sqlalchemy.exc import IntegrityError

        if isinstance(error, IntegrityError):
            raise conflict_from(error) from error
        raise
    await audit(
        db,
        admin,
        request,
        action="subscriber.create",
        entity_type="subscriber",
        entity_id=str(row["id"]),
        payload={"kind": kind},
    )
    return row


@router.get("/subscribers/{subscriber_id}")
async def get_subscriber(subscriber_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    """Карточка подписчика: профили, подписка, членство в чате, активность."""

    row = require(
        await fetch_one(
            db,
            text(
                "SELECT s.*, cm.bot_status, cm.can_post_messages, cm.member_count,"
                " cm.chat_type, cm.added_at, cm.removed_at"
                " FROM subscribers s"
                " LEFT JOIN chat_memberships cm ON cm.subscriber_id = s.id"
                " WHERE s.id = :id"
            ),
            {"id": str(subscriber_id)},
        ),
        "Подписчик",
    )
    profiles = await fetch_all(
        db,
        text(
            # description обязателен: редактор профиля в админке заполняет
            # поле из этого ответа и сохраняет его обратно. Без колонки поле
            # приходило пустым, и первое же сохранение стирало описание.
            "SELECT id, name, description, is_active, digest_enabled, delivery_format,"
            " max_items, min_personal_score, last_digest_at, next_digest_at, version"
            " FROM subscriber_profiles WHERE subscriber_id = :id"
            " ORDER BY is_active DESC, created_at"
        ),
        {"id": str(subscriber_id)},
    )
    subscription = await fetch_one(
        db,
        text(
            "SELECT sub.id, sub.status, sub.starts_at, sub.ends_at, sub.grace_until,"
            " p.key AS plan_key, p.name AS plan_name"
            " FROM subscriptions sub JOIN subscription_plans p ON p.id = sub.plan_id"
            " WHERE sub.subscriber_id = :id AND sub.status IN ('active', 'trial')"
            " AND sub.starts_at <= now()"
            " AND (sub.ends_at IS NULL OR coalesce(sub.grace_until, sub.ends_at) >= now())"
        ),
        {"id": str(subscriber_id)},
    )
    return {"subscriber": row, "profiles": profiles, "subscription": subscription}


@router.patch("/subscribers/{subscriber_id}")
async def patch_subscriber(
    subscriber_id: UUID, payload: SubscriberPatch, admin: Admin, db: Engine, request: Request
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нечего менять")
    if "status" in changes:
        changes["status"] = changes["status"].value
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    updated = await execute(
        db,
        text(f"UPDATE subscribers SET {assignments}, updated_at = now() WHERE id = :id"),
        {**changes, "id": str(subscriber_id)},
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Подписчик не найден")
    # Заблокированному подписчику дайджесты не строятся: иначе очередь копит
    # задания, которые Telegram всё равно отвергнет.
    if changes.get("status") in {UserStatus.BLOCKED.value, UserStatus.LEFT.value}:
        await execute(
            db,
            text(
                "UPDATE subscriber_profiles SET digest_enabled = false, updated_at = now()"
                " WHERE subscriber_id = :id"
            ),
            {"id": str(subscriber_id)},
        )
    await audit(
        db,
        admin,
        request,
        action="subscriber.update",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        payload=changes,
    )
    return {"updated": True, "changes": changes}


@router.post("/subscribers/{subscriber_id}/approve")
async def approve(
    subscriber_id: UUID,
    admin: Admin,
    db: Engine,
    repository: Subscribers,
    profiles: Profiles,
    container: AppContainer,
    settings: AppSettings,
    request: Request,
    payload: ApproveRequest | None = None,
) -> dict[str, Any]:
    """Впустить подписчика: статус, профиль, подписка и сообщение в Telegram.

    Все четыре шага — одно действие администратора, и разносить их по разным
    кнопкам нельзя. Подписчик без профиля не попадёт в дайджест, потому что
    нечего персонализировать; подписчик без действующей подписки не попадёт
    туда же, потому что так устроен диспетчер; а подписчик, которому не
    сказали, что его пустили, будет ждать вечно.

    Профиль, подписка и уведомление — не критичны по отдельности: если
    сорвалось что-то одно, статус всё равно уже сменился, и врать об этом
    хуже, чем вернуть отчёт с отметкой о неудаче.
    """

    options = payload or ApproveRequest()
    try:
        user = await repository.approve(subscriber_id, actor=admin.username)
    except SubscriberNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    report: dict[str, Any] = {"status": user.status.value, "kind": user.kind.value}

    try:
        profile = await profiles.ensure_profile(user.id)
        if options.description:
            profile = await profiles.update_profile(
                user.id, profile.id, description=options.description
            )
        report["profile_id"] = str(profile.id)
    except Exception as error:
        report["profile_error"] = str(error)[:300]

    if options.grant_trial and settings.default_trial_days:
        try:
            trial = await repository.start_trial(
                user.id,
                plan_key=settings.default_subscription_plan,
                trial_days=settings.default_trial_days,
                grace_days=settings.subscription_grace_days,
                actor=admin.username,
            )
            report["trial"] = trial.plan_key if trial else None
        except Exception as error:
            report["trial_error"] = str(error)[:300]

    if options.notify:
        report["notified"] = await _notify(
            container, user.telegram_chat_id, approval_message(user.kind)
        )

    await audit(
        db,
        admin,
        request,
        action="subscriber.approve",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        payload=report,
    )
    return {**user.model_dump(), "approval": report}


@router.post("/subscribers/{subscriber_id}/reject")
async def reject(
    subscriber_id: UUID,
    admin: Admin,
    db: Engine,
    repository: Subscribers,
    request: Request,
    payload: RejectRequest | None = None,
) -> dict[str, Any]:
    """Отказать в доступе. Подписчику ничего не отправляется намеренно.

    Сообщение «вам отказано» ничего не даёт получателю и приглашает спорить с
    ботом; заявка просто остаётся неподтверждённой, а причина — в аудите.
    """

    reason = (payload or RejectRequest()).reason
    try:
        user = await repository.reject(subscriber_id, actor=admin.username, reason=reason)
    except SubscriberNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscriber.reject",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        payload={"reason": reason},
    )
    return user.model_dump()


async def _notify(container: Any, chat_id: int, text: str) -> bool:
    """Написать подписчику. Неудача — это отчёт, а не отказ операции."""

    if not hasattr(container, "telegram_bot"):
        return False
    try:
        await container.telegram_bot().send_message(chat_id, text)
    except Exception:
        return False
    return True


@router.post("/subscribers/{subscriber_id}/block")
async def block(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> dict[str, Any]:
    try:
        user = await repository.set_status(subscriber_id, UserStatus.BLOCKED)
    except SubscriberNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="subscriber.block",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
    )
    return user.model_dump()


@router.delete("/subscribers/{subscriber_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscriber(
    subscriber_id: UUID, admin: Admin, db: Engine, repository: Subscribers, request: Request
) -> None:
    await repository.forget(subscriber_id)
    await audit(
        db,
        admin,
        request,
        action="subscriber.delete",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
    )


@router.get("/subscribers/{subscriber_id}/activity")
async def activity(subscriber_id: UUID, admin: Admin, db: Engine, paging: Paging) -> dict[str, Any]:
    """Лента событий подписчика: команды, фидбек, дайджесты, доставки."""

    rows = await fetch_all(
        db,
        text(
            "SELECT kind, payload, occurred_at, item_id, digest_id"
            " FROM subscriber_activity WHERE subscriber_id = :id"
            " ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
        ),
        {"id": str(subscriber_id), "limit": paging.limit, "offset": paging.offset},
    )
    total = await fetch_one(
        db,
        text("SELECT count(*) AS n FROM subscriber_activity WHERE subscriber_id = :id"),
        {"id": str(subscriber_id)},
    )
    return page_response(rows, int((total or {}).get("n", 0)), paging)


@router.post("/subscribers/{subscriber_id}/message")
async def send_message(
    subscriber_id: UUID,
    payload: MessageRequest,
    admin: Admin,
    db: Engine,
    request: Request,
) -> dict[str, Any]:
    """Отправить подписчику произвольное сообщение — минуя очередь дайджестов."""

    row = require(
        await fetch_one(
            db,
            text("SELECT telegram_chat_id, status FROM subscribers WHERE id = :id"),
            {"id": str(subscriber_id)},
        ),
        "Подписчик",
    )
    container = getattr(request.app.state, "container", None)
    if container is None or not hasattr(container, "telegram_bot"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Бот не сконфигурирован")
    try:
        message = await container.telegram_bot().send_message(row["telegram_chat_id"], payload.text)
    except Exception as error:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"Telegram отказал: {error}"
        ) from error
    await audit(
        db,
        admin,
        request,
        action="subscriber.message",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        payload={"length": len(payload.text)},
    )
    return {"sent": True, "message_id": getattr(message, "message_id", None)}


@router.get("/subscribers/{subscriber_id}/profiles")
async def list_profiles(subscriber_id: UUID, admin: Admin, db: Engine) -> list[dict[str, Any]]:
    return await fetch_all(
        db,
        text(
            "SELECT * FROM subscriber_profiles WHERE subscriber_id = :id"
            " ORDER BY is_active DESC, created_at"
        ),
        {"id": str(subscriber_id)},
    )


class ProfilePatch(BaseModel):
    #: Пустая строка очищает описание, `null` — «не трогать». Без этого различия
    #: описание нельзя было убрать вовсе: `exclude_none` выбрасывал поле, и
    #: сохранение пустого поля молча ничего не меняло.
    description: str | None = None
    digest_enabled: bool | None = None
    delivery_format: str | None = None
    max_items: int | None = Field(default=None, ge=1, le=100)
    min_personal_score: float | None = Field(default=None, ge=0, le=1)
    min_global_score: float | None = Field(default=None, ge=0, le=10)
    timezone: str | None = None
    paused_until: str | None = None


async def _profile_owner(db: Any, profile_id: UUID) -> UUID:
    """Владелец профиля. Сервис профилей работает парой (подписчик, профиль).

    Пара, а не один id, специально: так невозможно отредактировать чужой
    профиль, подставив угаданный UUID, — проверка владения делается внутри
    репозитория одной транзакцией с записью.
    """

    row = require(
        await fetch_one(
            db,
            text("SELECT subscriber_id FROM subscriber_profiles WHERE id = :id"),
            {"id": str(profile_id)},
        ),
        "Профиль",
    )
    return UUID(str(row["subscriber_id"]))


@router.post("/subscribers/{subscriber_id}/profiles", status_code=status.HTTP_201_CREATED)
async def create_profile(
    subscriber_id: UUID,
    payload: ProfileCreate,
    admin: Admin,
    db: Engine,
    profiles: Profiles,
    request: Request,
) -> dict[str, Any]:
    """Завести профиль подписчику — обычно группе, за которую пишет админ."""

    try:
        profile = await profiles.create_profile(
            subscriber_id,
            payload.name,
            description=payload.description,
            is_active=payload.is_active,
            digest_enabled=payload.digest_enabled,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="profile.create",
        entity_type="profile",
        entity_id=str(profile.id),
        payload={"subscriber_id": str(subscriber_id), "name": payload.name},
    )
    return profile.model_dump()


@router.patch("/profiles/{profile_id}")
async def patch_profile(
    profile_id: UUID,
    payload: ProfilePatch,
    admin: Admin,
    db: Engine,
    profiles: Profiles,
    request: Request,
) -> dict[str, Any]:
    """Правка профиля из админки.

    Описание и включение дайджеста идут через сервис профилей, а не прямым
    UPDATE: описание участвует в `compiled_text`, по которому работает
    ранжирование, и без перекомпиляции интерфейс показывал бы новое описание,
    а выдача продолжала бы жить по старому. Остальные поля — параметры
    доставки, на отбор они не влияют, и их можно писать напрямую.
    """

    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Нечего менять")
    if "delivery_format" in changes and changes["delivery_format"] not in {
        "cards",
        "compact",
        "single_message",
        "digest_post",
    }:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Неизвестный формат доставки")

    owner = await _profile_owner(db, profile_id)
    recompiled = ("description", "digest_enabled")
    compiled = {key: changes.pop(key) for key in recompiled if key in changes}
    if compiled:
        try:
            await profiles.update_profile(owner, profile_id, **compiled)
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    if changes:
        assignments = ", ".join(f"{key} = :{key}" for key in changes)
        updated = await execute(
            db,
            text(
                f"UPDATE subscriber_profiles SET {assignments}, updated_at = now() WHERE id = :id"
            ),
            {**changes, "id": str(profile_id)},
        )
        if not updated:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Профиль не найден")
    await audit(
        db,
        admin,
        request,
        action="profile.update",
        entity_type="profile",
        entity_id=str(profile_id),
        payload={**changes, **compiled},
    )
    return {"updated": True, "changes": {**changes, **compiled}}


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(
    profile_id: UUID, admin: Admin, db: Engine, profiles: Profiles, request: Request
) -> dict[str, Any]:
    owner = await _profile_owner(db, profile_id)
    profile = await profiles.activate_profile(owner, profile_id)
    await audit(
        db,
        admin,
        request,
        action="profile.activate",
        entity_type="profile",
        entity_id=str(profile_id),
    )
    return profile.model_dump()


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: UUID, admin: Admin, db: Engine, profiles: Profiles, request: Request
) -> dict[str, Any]:
    owner = await _profile_owner(db, profile_id)
    try:
        remaining = await profiles.delete_profile(owner, profile_id)
    except ValueError as error:
        # Последний профиль удалить нельзя: подписчик без профиля не получает
        # ничего и в интерфейсе выглядит как сломанный.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="profile.delete",
        entity_type="profile",
        entity_id=str(profile_id),
    )
    return {"deleted": True, "active": remaining.model_dump()}


@router.post("/profiles/{profile_id}/interests", status_code=status.HTTP_201_CREATED)
async def add_interest(
    profile_id: UUID,
    payload: InterestCreate,
    admin: Admin,
    db: Engine,
    profiles: Profiles,
    request: Request,
) -> dict[str, Any]:
    """Добавить тему в профиль. Перекомпиляция — внутри сервиса."""

    owner = await _profile_owner(db, profile_id)
    try:
        interest = await profiles.add_interest(
            owner,
            profile_id,
            query=payload.query,
            polarity=payload.polarity,
            weight=payload.weight,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    await audit(
        db,
        admin,
        request,
        action="profile.interest_add",
        entity_type="profile",
        entity_id=str(profile_id),
        payload={"query": payload.query, "polarity": payload.polarity.value},
    )
    return interest.model_dump()


@router.delete("/profiles/{profile_id}/interests/{interest_id}")
async def remove_interest(
    profile_id: UUID,
    interest_id: UUID,
    admin: Admin,
    db: Engine,
    profiles: Profiles,
    request: Request,
) -> dict[str, Any]:
    owner = await _profile_owner(db, profile_id)
    await profiles.remove_interest(owner, profile_id, interest_id)
    await audit(
        db,
        admin,
        request,
        action="profile.interest_remove",
        entity_type="profile",
        entity_id=str(profile_id),
        payload={"interest_id": str(interest_id)},
    )
    return {"deleted": True}


@router.get("/profiles/guide")
async def profile_guide(admin: Admin) -> dict[str, Any]:
    """Инструкция «как писать профиль» — тот же текст, что показывает бот.

    Отдаётся из API, а не набирается во фронте: два текста в двух местах
    однажды разойдутся, и правило, объяснённое админке иначе, чем человеку в
    Telegram, хуже отсутствующего.
    """

    return {"sections": as_payload()}


@router.post("/profiles/preview")
async def preview_facets(
    payload: PreviewRequest, admin: Admin, db: Engine, settings: AppSettings
) -> dict[str, Any]:
    """На какие темы разобьётся черновик описания.

    Разбиение механическое и из поля ввода не видно. Показать результат до
    сохранения дешевле, чем объяснить правило: человек исправляет профиль
    сразу, а не после месяца не того дайджеста.
    """

    blocks = [f"{DESCRIPTION_SECTION}:\n{payload.description.strip()}"]
    if payload.profile_id is not None:
        rows = await fetch_all(
            db,
            text(
                "SELECT i.polarity, coalesce(t.name, i.query) AS target, i.weight"
                " FROM profile_interests i LEFT JOIN topics t ON t.id = i.topic_id"
                " WHERE i.profile_id = :id ORDER BY i.weight DESC"
            ),
            {"id": str(payload.profile_id)},
        )
        if rows:
            # Собираем ровно тот же раздел, что пишет компилятор профиля:
            # предпросмотр обязан разбираться тем же кодом, что и поиск.
            lines = "\n".join(
                f"- {row['polarity']}: {row['target']} (weight={float(row['weight']):g})"
                for row in rows
                if row["target"]
            )
            blocks.append(f"{INTERESTS_SECTION}:\n{lines}")
    result = preview(
        "\n\n".join(blocks),
        facet_limit=settings.profile_facet_limit,
        facet_min_chars=settings.profile_facet_min_chars,
    )
    return _preview_payload(result)


@router.get("/profiles/{profile_id}/preview")
async def profile_preview(
    profile_id: UUID, admin: Admin, db: Engine, settings: AppSettings
) -> dict[str, Any]:
    """То же, но для сохранённого профиля — вместе с явными темами."""

    row = require(
        await fetch_one(
            db,
            text("SELECT compiled_text FROM subscriber_profiles WHERE id = :id"),
            {"id": str(profile_id)},
        ),
        "Профиль",
    )
    result = preview(
        str(row["compiled_text"] or ""),
        facet_limit=settings.profile_facet_limit,
        facet_min_chars=settings.profile_facet_min_chars,
    )
    return _preview_payload(result)


def _preview_payload(result: Any) -> dict[str, Any]:
    return {
        "facets": [
            {"index": facet.index, "text": facet.text, "source": facet.source}
            for facet in result.facets
        ],
        # Что не стало темой: короткий обрывок приклеился к соседнему или не
        # прошёл вовсе. Молча пропавший кусок описания — самая обидная из
        # ошибок: человек его написал и уверен, что он работает.
        "dropped": list(result.dropped),
        "notes": [
            {"level": note.level, "text": note.text, "subject": note.subject}
            for note in result.notes
        ],
    }


@router.get("/profiles/{profile_id}/interests")
async def interests(profile_id: UUID, admin: Admin, db: Engine) -> dict[str, Any]:
    explicit = await fetch_all(
        db,
        text(
            "SELECT i.id, i.query, i.polarity, i.weight, t.name AS topic"
            " FROM profile_interests i LEFT JOIN topics t ON t.id = i.topic_id"
            " WHERE i.profile_id = :id ORDER BY i.weight DESC"
        ),
        {"id": str(profile_id)},
    )
    learned = await fetch_all(
        db,
        text(
            "SELECT s.id, s.query, s.polarity, s.weight, s.evidence_count, s.source"
            " FROM profile_interest_signals s WHERE s.profile_id = :id"
            " ORDER BY s.weight DESC LIMIT 100"
        ),
        {"id": str(profile_id)},
    )
    return {"explicit": explicit, "learned": learned}
