"""Подписчики, чаты и подписки: выборки, различающие людей, группы и каналы.

Один и тот же `subscribers` держит и личный чат, и группу, и канал — разводит
их колонка `kind`. Поэтому почти каждая выборка здесь принимает набор видов:
рассылка в личку и рассылка в группы читают одну таблицу, но разные строки, и
смешивать их нельзя ни в лимитах Bot API, ни в правах, ни в статистике.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from geonexa_proxima.db.models import (
    ChatEventModel,
    ChatMembershipModel,
    SubscriberActivityModel,
    SubscriberModel,
    SubscriberProfileModel,
    SubscriptionEventModel,
    SubscriptionModel,
    SubscriptionPlanModel,
)
from geonexa_proxima.db.session import SessionFactory
from geonexa_proxima.domain import (
    ABSENT_BOT_STATUSES,
    ALL_KINDS,
    CHAT_KINDS,
    PERSONAL_KINDS,
    PRESENT_BOT_STATUSES,
    NotFoundError,
    SubscriberKind,
    User,
    UserStatus,
)

_WHITESPACE = re.compile(r"\s+")

ACTIVE_SUBSCRIPTION_STATUSES: tuple[str, ...] = ("active", "trial")

#: Telegram-тип чата -> наш `kind`. Всё, что не канал, считаем группой.
_CHAT_TYPE_TO_KIND = {
    "group": SubscriberKind.GROUP.value,
    "supergroup": SubscriberKind.GROUP.value,
    "channel": SubscriberKind.CHANNEL.value,
    "private": SubscriberKind.USER.value,
}


class SubscriberNotFoundError(NotFoundError):
    """Подписчик не найден."""


class SubscriptionNotFoundError(NotFoundError):
    """Подписка не найдена."""


class PlanNotFoundError(NotFoundError):
    """Тариф не найден."""


class SubscriptionOverlapError(ValueError):
    """Периоды действующих подписок пересекаются — запрещено БД."""


def kind_from_chat_type(chat_type: str | None) -> str:
    """Определить вид подписчика по `chat.type` из Telegram."""

    return _CHAT_TYPE_TO_KIND.get((chat_type or "").strip().lower(), SubscriberKind.GROUP.value)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())
    return cleaned or None


def _kinds(value: Iterable[str | SubscriberKind] | None, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    resolved = tuple(str(getattr(item, "value", item)) for item in value)
    if not resolved:
        raise ValueError("список видов подписчиков не может быть пустым")
    unknown = set(resolved) - {kind.value for kind in SubscriberKind}
    if unknown:
        raise ValueError("неизвестный вид подписчика: " + ", ".join(sorted(unknown)))
    return resolved


def _statuses(value: Iterable[str | UserStatus] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    resolved = tuple(str(getattr(item, "value", item)) for item in value)
    if not resolved:
        raise ValueError("список статусов не может быть пустым")
    unknown = set(resolved) - {status.value for status in UserStatus}
    if unknown:
        raise ValueError("неизвестный статус подписчика: " + ", ".join(sorted(unknown)))
    return resolved


@dataclass(slots=True, frozen=True)
class ChatIdentity:
    """То, что Telegram сообщает о чате, куда добавили бота."""

    telegram_chat_id: int
    chat_type: str | None = None
    title: str | None = None
    username: str | None = None
    added_by_user_id: int | None = None
    invite_link: str | None = None

    @property
    def kind(self) -> str:
        return kind_from_chat_type(self.chat_type)


@dataclass(slots=True)
class ChatRecord:
    """Чат вместе с тем, что бот в нём может."""

    subscriber_id: UUID
    kind: str
    telegram_chat_id: int
    title: str | None
    username: str | None
    status: str
    bot_status: str
    chat_type: str | None
    member_count: int | None
    can_post_messages: bool | None
    added_by_user_id: int | None
    added_at: datetime | None
    removed_at: datetime | None
    last_checked_at: datetime | None
    error: str | None
    profiles: int = 0

    @property
    def is_present(self) -> bool:
        return self.bot_status in PRESENT_BOT_STATUSES

    @property
    def can_deliver(self) -> bool:
        """Канал без права постить — это чат, в который нельзя слать дайджест."""

        if not self.is_present:
            return False
        if self.kind == SubscriberKind.CHANNEL.value:
            return bool(self.can_post_messages)
        return True


@dataclass(slots=True)
class SubscriptionRecord:
    id: UUID
    subscriber_id: UUID
    plan_id: UUID
    plan_key: str
    plan_name: str
    status: str
    starts_at: datetime
    ends_at: datetime | None
    grace_until: datetime | None
    auto_renew: bool
    source: str
    notes: str | None
    created_at: datetime

    def is_running(self, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        if self.status not in ACTIVE_SUBSCRIPTION_STATUSES:
            return False
        if self.starts_at > moment:
            return False
        deadline = self.grace_until or self.ends_at
        return deadline is None or deadline >= moment


@dataclass(slots=True)
class PlanLimits:
    """Что подписчику разрешено. Без подписки — тариф по умолчанию."""

    plan_key: str
    plan_name: str
    max_profiles: int
    max_items_per_digest: int
    min_interval_hours: int
    deep_analysis_quota_per_month: int
    allow_group_chats: bool
    features: dict[str, Any] = field(default_factory=dict)
    subscription_id: UUID | None = None
    status: str = "none"
    ends_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.subscription_id is not None


@dataclass(slots=True, frozen=True)
class KindBreakdown:
    """Сколько подписчиков каждого вида и в каком они статусе."""

    kind: str
    status: str
    count: int


class SubscriberRepository:
    """Транзакционный доступ к подписчикам, чатам и подпискам.

    Все методы открывают собственную сессию — репозиторий безопасно держать
    как синглтон рядом с общим движком.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # Выборки, различающие вид подписчика                                 #
    # ------------------------------------------------------------------ #

    async def get(self, subscriber_id: UUID) -> User | None:
        async with self._session_factory() as session:
            model = await session.get(SubscriberModel, subscriber_id)
            return _to_user(model) if model else None

    async def get_by_chat_id(self, telegram_chat_id: int) -> User | None:
        async with self._session_factory() as session:
            model = await session.scalar(
                select(SubscriberModel).where(SubscriberModel.telegram_chat_id == telegram_chat_id)
            )
            return _to_user(model) if model else None

    async def require(self, subscriber_id: UUID) -> User:
        user = await self.get(subscriber_id)
        if user is None:
            raise SubscriberNotFoundError(f"Подписчик {subscriber_id} не найден")
        return user

    async def list_subscribers(
        self,
        *,
        kinds: Iterable[str | SubscriberKind] | None = None,
        statuses: Iterable[str | UserStatus] | None = None,
        search: str | None = None,
        with_active_subscription: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[User]:
        """Постраничная выборка подписчиков нужных видов."""

        statement = self._filtered(
            select(SubscriberModel),
            kinds=kinds,
            statuses=statuses,
            search=search,
            with_active_subscription=with_active_subscription,
        ).order_by(SubscriberModel.created_at.desc(), SubscriberModel.id)
        statement = statement.limit(max(1, min(limit, 500))).offset(max(0, offset))
        async with self._session_factory() as session:
            models = (await session.scalars(statement)).all()
            return [_to_user(model) for model in models]

    async def count_subscribers(
        self,
        *,
        kinds: Iterable[str | SubscriberKind] | None = None,
        statuses: Iterable[str | UserStatus] | None = None,
        search: str | None = None,
        with_active_subscription: bool | None = None,
    ) -> int:
        statement = self._filtered(
            select(func.count()).select_from(SubscriberModel),
            kinds=kinds,
            statuses=statuses,
            search=search,
            with_active_subscription=with_active_subscription,
        )
        async with self._session_factory() as session:
            return int(await session.scalar(statement) or 0)

    async def breakdown(self) -> list[KindBreakdown]:
        """Разрез «вид × статус» — то, что показывает верх админки."""

        statement = (
            select(SubscriberModel.kind, SubscriberModel.status, func.count())
            .group_by(SubscriberModel.kind, SubscriberModel.status)
            .order_by(SubscriberModel.kind, SubscriberModel.status)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [KindBreakdown(kind=row[0], status=row[1], count=int(row[2])) for row in rows]

    async def list_delivery_targets(
        self,
        *,
        kinds: Iterable[str | SubscriberKind] | None = None,
        limit: int = 500,
    ) -> list[User]:
        """Активные подписчики нужных видов с действующей подпиской.

        Отдельный метод, а не флаг в `list_subscribers`, потому что для чатов
        добавляется проверка присутствия бота: подписка у группы может быть
        живой, а бота из неё уже выгнали.
        """

        resolved = _kinds(kinds, PERSONAL_KINDS)
        statement = self._filtered(
            select(SubscriberModel),
            kinds=resolved,
            statuses=(UserStatus.ACTIVE.value,),
            with_active_subscription=True,
        )
        if set(resolved) & set(CHAT_KINDS):
            statement = statement.where(
                SubscriberModel.id.in_(
                    select(ChatMembershipModel.subscriber_id).where(
                        ChatMembershipModel.bot_status.in_(PRESENT_BOT_STATUSES)
                    )
                )
            )
        statement = statement.order_by(SubscriberModel.created_at).limit(max(1, limit))
        async with self._session_factory() as session:
            models = (await session.scalars(statement)).all()
            return [_to_user(model) for model in models]

    def _filtered(
        self,
        statement: Select[Any],
        *,
        kinds: Iterable[str | SubscriberKind] | None,
        statuses: Iterable[str | UserStatus] | None = None,
        search: str | None = None,
        with_active_subscription: bool | None = None,
    ) -> Select[Any]:
        resolved_kinds = _kinds(kinds, ALL_KINDS)
        statement = statement.where(SubscriberModel.kind.in_(resolved_kinds))
        resolved_statuses = _statuses(statuses)
        if resolved_statuses is not None:
            statement = statement.where(SubscriberModel.status.in_(resolved_statuses))
        cleaned = _clean(search)
        if cleaned:
            pattern = f"%{cleaned.lower()}%"
            conditions = [
                func.lower(func.coalesce(SubscriberModel.title, "")).like(pattern),
                func.lower(func.coalesce(SubscriberModel.telegram_username, "")).like(pattern),
            ]
            if cleaned.lstrip("-").isdigit():
                conditions.append(SubscriberModel.telegram_chat_id == int(cleaned))
            statement = statement.where(or_(*conditions))
        if with_active_subscription is not None:
            exists = (
                select(SubscriptionModel.id)
                .where(
                    SubscriptionModel.subscriber_id == SubscriberModel.id,
                    SubscriptionModel.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
                    SubscriptionModel.starts_at <= func.now(),
                    or_(
                        SubscriptionModel.ends_at.is_(None),
                        func.coalesce(SubscriptionModel.grace_until, SubscriptionModel.ends_at)
                        >= func.now(),
                    ),
                )
                .exists()
            )
            statement = statement.where(exists if with_active_subscription else ~exists)
        return statement

    # ------------------------------------------------------------------ #
    # Регистрация групп и каналов                                         #
    # ------------------------------------------------------------------ #

    async def register_chat(
        self,
        identity: ChatIdentity,
        *,
        bot_status: str = "member",
        can_post_messages: bool | None = None,
        member_count: int | None = None,
        raw_update: dict[str, Any] | None = None,
        initial_status: str | UserStatus = UserStatus.PENDING,
    ) -> tuple[User, bool]:
        """Завести (или обновить) группу либо канал вместе с членством бота.

        Возвращает подписчика и признак «создан впервые». Идемпотентно: бота
        добавляют и удаляют по многу раз, и каждый раз это один и тот же чат.

        Новый чат заводится неактивным: присутствие бота в группе — это ещё не
        разрешение туда писать. Разрешение даёт администратор в админке, и
        отметка об этом живёт в ``meta.approved``. Повторное добавление бота в
        уже одобренный чат возвращает его в работу без второго подтверждения.
        """

        kind = identity.kind
        if kind not in CHAT_KINDS:
            raise ValueError("register_chat принимает только группы и каналы")
        if identity.telegram_chat_id >= 0:
            raise ValueError("chat_id группы или канала в Telegram отрицателен")

        now = datetime.now(UTC)
        present = bot_status in PRESENT_BOT_STATUSES
        values = {
            "kind": kind,
            "telegram_chat_id": identity.telegram_chat_id,
            "title": _clean(identity.title),
            "telegram_username": _clean(identity.username),
            "status": (
                str(getattr(initial_status, "value", initial_status))
                if present
                else UserStatus.LEFT.value
            ),
        }
        async with self._session_factory() as session, session.begin():
            statement = (
                insert(SubscriberModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[SubscriberModel.telegram_chat_id])
                .returning(SubscriberModel)
            )
            model = (await session.scalars(statement)).one_or_none()
            created = model is not None
            if model is None:
                model = await session.scalar(
                    select(SubscriberModel)
                    .where(SubscriberModel.telegram_chat_id == identity.telegram_chat_id)
                    .with_for_update()
                )
                if model is None:  # pragma: no cover — гонка с удалением
                    raise SubscriberNotFoundError(
                        f"Чат {identity.telegram_chat_id} исчез во время регистрации"
                    )
                model.kind = kind
                if values["title"]:
                    model.title = values["title"]
                model.telegram_username = values["telegram_username"]
                model.last_seen_at = now
                model.updated_at = now
                # Возвращаем в работу только чат, который из неё выпал. Иначе
                # повторное добавление бота снимало бы блокировку, поставленную
                # администратором, — и выгнанный чат возвращался бы сам.
                if present and model.status == UserStatus.LEFT.value:
                    model.status = _status_after_return(model)
                await session.flush()

            previous = await self._upsert_membership(
                session,
                model.id,
                bot_status=bot_status,
                chat_type=identity.chat_type,
                can_post_messages=can_post_messages,
                member_count=member_count,
                added_by_user_id=identity.added_by_user_id,
                invite_link=identity.invite_link,
                now=now,
            )
            session.add(
                ChatEventModel(
                    subscriber_id=model.id,
                    event_type="registered" if created else "updated",
                    old_value=previous,
                    new_value={"bot_status": bot_status, "kind": kind},
                    raw_update=raw_update,
                )
            )
            session.add(
                SubscriberActivityModel(
                    subscriber_id=model.id,
                    kind="chat_joined" if bot_status in PRESENT_BOT_STATUSES else "chat_left",
                    payload={"bot_status": bot_status, "kind": kind},
                )
            )
            user = _to_user(model)
        return user, created

    async def update_bot_status(
        self,
        telegram_chat_id: int,
        bot_status: str,
        *,
        can_post_messages: bool | None = None,
        member_count: int | None = None,
        error: str | None = None,
        raw_update: dict[str, Any] | None = None,
    ) -> ChatRecord:
        """Записать смену прав бота в чате и, если бота выгнали, погасить чат."""

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            model = await session.scalar(
                select(SubscriberModel)
                .where(SubscriberModel.telegram_chat_id == telegram_chat_id)
                .with_for_update()
            )
            if model is None:
                raise SubscriberNotFoundError(f"Чат {telegram_chat_id} не зарегистрирован")
            previous = await self._upsert_membership(
                session,
                model.id,
                bot_status=bot_status,
                chat_type=None,
                can_post_messages=can_post_messages,
                member_count=member_count,
                added_by_user_id=None,
                invite_link=None,
                error=error,
                now=now,
            )
            if bot_status in ABSENT_BOT_STATUSES:
                # Блокировку выход бота не снимает. Иначе заблокированному чату
                # достаточно выгнать бота и добавить обратно, чтобы вернуться в
                # рассылку: `left` затирал бы `blocked`, а обратный переход
                # ниже видит только `left` и открывает доступ.
                if model.status != UserStatus.BLOCKED.value:
                    model.status = UserStatus.LEFT.value
                await session.execute(
                    update(SubscriberProfileModel)
                    .where(SubscriberProfileModel.subscriber_id == model.id)
                    .values(digest_enabled=False)
                )
            elif model.status == UserStatus.LEFT.value:
                model.status = _status_after_return(model)
            model.updated_at = now
            session.add(
                ChatEventModel(
                    subscriber_id=model.id,
                    event_type="bot_status_changed",
                    old_value=previous,
                    new_value={"bot_status": bot_status},
                    raw_update=raw_update,
                )
            )
            await session.flush()
        record = await self.get_chat(telegram_chat_id)
        assert record is not None
        return record

    async def _upsert_membership(
        self,
        session: AsyncSession,
        subscriber_id: UUID,
        *,
        bot_status: str,
        chat_type: str | None,
        can_post_messages: bool | None,
        member_count: int | None,
        added_by_user_id: int | None,
        invite_link: str | None,
        now: datetime,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        membership = await session.scalar(
            select(ChatMembershipModel)
            .where(ChatMembershipModel.subscriber_id == subscriber_id)
            .with_for_update()
        )
        previous = (
            {
                "bot_status": membership.bot_status,
                "can_post_messages": membership.can_post_messages,
                "member_count": membership.member_count,
            }
            if membership is not None
            else None
        )
        present = bot_status in PRESENT_BOT_STATUSES
        if membership is None:
            membership = ChatMembershipModel(
                subscriber_id=subscriber_id,
                bot_status=bot_status,
                chat_type=chat_type,
                can_post_messages=can_post_messages,
                member_count=member_count,
                added_by_user_id=added_by_user_id,
                invite_link=invite_link,
                added_at=now if present else None,
                removed_at=None if present else now,
                last_checked_at=now,
                error=error,
            )
            session.add(membership)
        else:
            membership.bot_status = bot_status
            if chat_type is not None:
                membership.chat_type = chat_type
            if can_post_messages is not None:
                membership.can_post_messages = can_post_messages
            if member_count is not None:
                membership.member_count = member_count
            if added_by_user_id is not None:
                membership.added_by_user_id = added_by_user_id
            if invite_link is not None:
                membership.invite_link = invite_link
            if present:
                membership.added_at = membership.added_at or now
                membership.removed_at = None
            else:
                membership.removed_at = now
            membership.last_checked_at = now
            membership.error = error
            membership.updated_at = now
        await session.flush()
        return previous

    async def get_chat(self, telegram_chat_id: int) -> ChatRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    self._chat_select().where(SubscriberModel.telegram_chat_id == telegram_chat_id)
                )
            ).first()
            return _to_chat(row) if row else None

    async def list_chats(
        self,
        *,
        kinds: Iterable[str | SubscriberKind] | None = None,
        bot_statuses: Iterable[str] | None = None,
        present_only: bool = False,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ChatRecord]:
        """Все чаты, куда добавляли бота, вместе с его текущими правами."""

        statement = self._chat_select()
        statement = self._filtered(statement, kinds=_kinds(kinds, CHAT_KINDS), search=search)
        if present_only:
            statement = statement.where(ChatMembershipModel.bot_status.in_(PRESENT_BOT_STATUSES))
        elif bot_statuses is not None:
            resolved = tuple(bot_statuses)
            if not resolved:
                raise ValueError("список статусов бота не может быть пустым")
            statement = statement.where(ChatMembershipModel.bot_status.in_(resolved))
        statement = (
            statement.order_by(SubscriberModel.created_at.desc(), SubscriberModel.id)
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            return [_to_chat(row) for row in rows]

    def _chat_select(self) -> Select[Any]:
        profiles = (
            select(func.count())
            .select_from(SubscriberProfileModel)
            .where(SubscriberProfileModel.subscriber_id == SubscriberModel.id)
            .scalar_subquery()
        )
        return (
            select(SubscriberModel, ChatMembershipModel, profiles.label("profiles"))
            .join(
                ChatMembershipModel,
                ChatMembershipModel.subscriber_id == SubscriberModel.id,
                isouter=True,
            )
            .where(SubscriberModel.kind.in_(CHAT_KINDS))
        )

    async def chat_events(self, subscriber_id: UUID, *, limit: int = 50) -> list[ChatEventModel]:
        async with self._session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(ChatEventModel)
                        .where(ChatEventModel.subscriber_id == subscriber_id)
                        .order_by(ChatEventModel.occurred_at.desc())
                        .limit(max(1, min(limit, 500)))
                    )
                ).all()
            )

    # ------------------------------------------------------------------ #
    # Подписки                                                            #
    # ------------------------------------------------------------------ #

    async def list_plans(self, *, enabled_only: bool = True) -> list[SubscriptionPlanModel]:
        statement = select(SubscriptionPlanModel).order_by(SubscriptionPlanModel.min_interval_hours)
        if enabled_only:
            statement = statement.where(SubscriptionPlanModel.enabled.is_(True))
        async with self._session_factory() as session:
            return list((await session.scalars(statement)).all())

    async def default_plan(self) -> SubscriptionPlanModel:
        async with self._session_factory() as session:
            plan = await session.scalar(
                select(SubscriptionPlanModel).where(SubscriptionPlanModel.is_default.is_(True))
            )
        if plan is None:
            raise PlanNotFoundError("Тариф по умолчанию не настроен")
        return plan

    async def current_subscription(self, subscriber_id: UUID) -> SubscriptionRecord | None:
        """Действующая подписка — та, чей период накрывает текущий момент."""

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    self._subscription_select().where(
                        SubscriptionModel.subscriber_id == subscriber_id,
                        SubscriptionModel.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
                        SubscriptionModel.starts_at <= func.now(),
                        or_(
                            SubscriptionModel.ends_at.is_(None),
                            func.coalesce(SubscriptionModel.grace_until, SubscriptionModel.ends_at)
                            >= func.now(),
                        ),
                    )
                )
            ).first()
            return _to_subscription(row) if row else None

    async def list_subscriptions(
        self, subscriber_id: UUID, *, limit: int = 50
    ) -> list[SubscriptionRecord]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    self._subscription_select()
                    .where(SubscriptionModel.subscriber_id == subscriber_id)
                    .order_by(SubscriptionModel.starts_at.desc())
                    .limit(max(1, min(limit, 500)))
                )
            ).all()
            return [_to_subscription(row) for row in rows]

    async def grant_subscription(
        self,
        subscriber_id: UUID,
        plan_key: str,
        *,
        status: str = "active",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        duration: timedelta | None = None,
        grace: timedelta | None = None,
        auto_renew: bool = False,
        source: str = "admin",
        notes: str | None = None,
        actor: str | None = None,
        replace_current: bool = True,
    ) -> SubscriptionRecord:
        """Выдать подписку, закрыв предыдущую.

        В БД стоит exclusion-ограничение на пересечение действующих периодов,
        поэтому «выдать вторую подписку поверх первой» — не операция, а ошибка.
        Либо предыдущая закрывается здесь же, либо вызов падает.
        """

        if status not in ACTIVE_SUBSCRIPTION_STATUSES and status != "pending":
            raise ValueError("выдавать можно только pending, trial или active")
        now = datetime.now(UTC)
        begin = starts_at or now
        finish = ends_at if ends_at is not None else (begin + duration if duration else None)
        if finish is not None and finish <= begin:
            raise ValueError("окончание подписки должно быть позже начала")

        async with self._session_factory() as session, session.begin():
            subscriber = await session.scalar(
                select(SubscriberModel).where(SubscriberModel.id == subscriber_id).with_for_update()
            )
            if subscriber is None:
                raise SubscriberNotFoundError(f"Подписчик {subscriber_id} не найден")
            plan = await session.scalar(
                select(SubscriptionPlanModel).where(SubscriptionPlanModel.key == plan_key)
            )
            if plan is None:
                raise PlanNotFoundError(f"Тариф {plan_key!r} не найден")
            if subscriber.kind in CHAT_KINDS and not plan.allow_group_chats:
                raise ValueError(f"Тариф {plan_key!r} не разрешает групповые чаты")

            overlapping = list(
                (
                    await session.scalars(
                        select(SubscriptionModel)
                        .where(
                            SubscriptionModel.subscriber_id == subscriber_id,
                            SubscriptionModel.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
                            or_(
                                SubscriptionModel.ends_at.is_(None),
                                SubscriptionModel.ends_at > begin,
                            ),
                        )
                        .with_for_update()
                    )
                ).all()
            )
            if overlapping and not replace_current:
                raise SubscriptionOverlapError(
                    "У подписчика уже есть действующая подписка на этот период"
                )
            for previous in overlapping:
                previous.status = "cancelled"
                previous.ends_at = min(previous.ends_at or begin, begin)
                previous.updated_at = now
                session.add(
                    SubscriptionEventModel(
                        subscription_id=previous.id,
                        event="cancelled",
                        payload={"reason": "replaced", "plan": plan_key},
                        actor=actor,
                    )
                )
            await session.flush()

            subscription = SubscriptionModel(
                subscriber_id=subscriber_id,
                plan_id=plan.id,
                status=status,
                starts_at=begin,
                ends_at=finish,
                grace_until=(finish + grace) if (finish and grace) else None,
                auto_renew=auto_renew,
                source=source,
                notes=_clean(notes),
                created_by=actor,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                SubscriptionEventModel(
                    subscription_id=subscription.id,
                    event="created",
                    payload={"plan": plan_key, "status": status},
                    actor=actor,
                )
            )
            session.add(
                SubscriberActivityModel(
                    subscriber_id=subscriber_id,
                    kind="subscription_changed",
                    payload={"plan": plan_key, "status": status},
                )
            )
            record = SubscriptionRecord(
                id=subscription.id,
                subscriber_id=subscriber_id,
                plan_id=plan.id,
                plan_key=plan.key,
                plan_name=plan.name,
                status=status,
                starts_at=begin,
                ends_at=finish,
                grace_until=subscription.grace_until,
                auto_renew=auto_renew,
                source=source,
                notes=subscription.notes,
                created_at=now,
            )
        return record

    async def extend_subscription(
        self,
        subscription_id: UUID,
        *,
        until: datetime | None = None,
        by: timedelta | None = None,
        actor: str | None = None,
    ) -> SubscriptionRecord:
        if (until is None) == (by is None):
            raise ValueError("укажите либо until, либо by")
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(SubscriptionModel)
                .where(SubscriptionModel.id == subscription_id)
                .with_for_update()
            )
            if subscription is None:
                raise SubscriptionNotFoundError(f"Подписка {subscription_id} не найдена")
            previous_end = subscription.ends_at
            if until is not None:
                new_end: datetime | None = until
            else:
                assert by is not None
                base = previous_end or now
                new_end = base + by
            if new_end is not None and new_end <= subscription.starts_at:
                raise ValueError("окончание подписки должно быть позже начала")
            subscription.ends_at = new_end
            if subscription.status == "expired":
                subscription.status = "active"
            subscription.updated_at = now
            session.add(
                SubscriptionEventModel(
                    subscription_id=subscription.id,
                    event="extended",
                    payload={
                        "from": previous_end.isoformat() if previous_end else None,
                        "to": new_end.isoformat() if new_end else None,
                    },
                    actor=actor,
                )
            )
            await session.flush()
        result = await self.get_subscription(subscription_id)
        assert result is not None
        return result

    async def cancel_subscription(
        self, subscription_id: UUID, *, actor: str | None = None, reason: str | None = None
    ) -> SubscriptionRecord:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(SubscriptionModel)
                .where(SubscriptionModel.id == subscription_id)
                .with_for_update()
            )
            if subscription is None:
                raise SubscriptionNotFoundError(f"Подписка {subscription_id} не найдена")
            subscription.status = "cancelled"
            if subscription.ends_at is None or subscription.ends_at > now:
                subscription.ends_at = max(now, subscription.starts_at)
            subscription.auto_renew = False
            subscription.updated_at = now
            session.add(
                SubscriptionEventModel(
                    subscription_id=subscription.id,
                    event="cancelled",
                    payload={"reason": _clean(reason)},
                    actor=actor,
                )
            )
            await session.flush()
        result = await self.get_subscription(subscription_id)
        assert result is not None
        return result

    async def get_subscription(self, subscription_id: UUID) -> SubscriptionRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    self._subscription_select().where(SubscriptionModel.id == subscription_id)
                )
            ).first()
            return _to_subscription(row) if row else None

    async def expire_due(self, *, now: datetime | None = None) -> int:
        """Перевести просроченные подписки в `expired` и записать событие."""

        moment = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            due = list(
                (
                    await session.scalars(
                        select(SubscriptionModel)
                        .where(
                            SubscriptionModel.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
                            SubscriptionModel.ends_at.is_not(None),
                            func.coalesce(SubscriptionModel.grace_until, SubscriptionModel.ends_at)
                            < moment,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for subscription in due:
                subscription.status = "expired"
                subscription.updated_at = moment
                session.add(
                    SubscriptionEventModel(
                        subscription_id=subscription.id,
                        event="expired",
                        payload={"at": moment.isoformat()},
                        actor="system",
                    )
                )
            await session.flush()
        return len(due)

    async def list_expiring(
        self, *, within: timedelta, kinds: Iterable[str | SubscriberKind] | None = None
    ) -> list[SubscriptionRecord]:
        """Подписки, которые закончатся в ближайшее окно, — для напоминаний."""

        deadline = datetime.now(UTC) + within
        statement = (
            self._subscription_select()
            .join(SubscriberModel, SubscriberModel.id == SubscriptionModel.subscriber_id)
            .where(
                SubscriptionModel.status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
                SubscriptionModel.ends_at.is_not(None),
                SubscriptionModel.ends_at <= deadline,
                SubscriptionModel.ends_at >= func.now(),
                SubscriberModel.kind.in_(_kinds(kinds, ALL_KINDS)),
            )
            .order_by(SubscriptionModel.ends_at)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            return [_to_subscription(row) for row in rows]

    async def start_trial(
        self,
        subscriber_id: UUID,
        *,
        plan_key: str,
        trial_days: int,
        grace_days: int = 0,
        actor: str | None = None,
    ) -> SubscriptionRecord | None:
        """Выдать пробный период новичку.

        Возвращает None, если подписка уже есть: повторный триал — не ошибка
        вызывающего, а обычный случай (человек нажал /start второй раз), и
        падать на нём нельзя. `trial_days=0` означает, что пробного периода в
        этой установке нет.
        """

        if trial_days <= 0:
            return None
        if await self.current_subscription(subscriber_id) is not None:
            return None
        if await self._had_trial(subscriber_id):
            return None
        return await self.grant_subscription(
            subscriber_id,
            plan_key,
            status="trial",
            duration=timedelta(days=trial_days),
            grace=timedelta(days=grace_days) if grace_days else None,
            source="trial",
            actor=actor or "system",
        )

    async def _had_trial(self, subscriber_id: UUID) -> bool:
        """Один пробный период на подписчика: иначе он вечный."""

        async with self._session_factory() as session:
            found = await session.scalar(
                select(SubscriptionModel.id).where(
                    SubscriptionModel.subscriber_id == subscriber_id,
                    SubscriptionModel.source == "trial",
                )
            )
        return found is not None

    async def limits(self, subscriber_id: UUID) -> PlanLimits:
        """Действующие лимиты подписчика — из подписки либо из тарифа по умолчанию."""

        current = await self.current_subscription(subscriber_id)
        async with self._session_factory() as session:
            if current is not None:
                plan = await session.get(SubscriptionPlanModel, current.plan_id)
            else:
                plan = await session.scalar(
                    select(SubscriptionPlanModel).where(SubscriptionPlanModel.is_default.is_(True))
                )
        if plan is None:
            raise PlanNotFoundError("Тариф по умолчанию не настроен")
        return PlanLimits(
            plan_key=plan.key,
            plan_name=plan.name,
            max_profiles=plan.max_profiles,
            max_items_per_digest=plan.max_items_per_digest,
            min_interval_hours=plan.min_interval_hours,
            deep_analysis_quota_per_month=plan.deep_analysis_quota_per_month,
            allow_group_chats=plan.allow_group_chats,
            features=dict(plan.features or {}),
            subscription_id=current.id if current else None,
            status=current.status if current else "none",
            ends_at=current.ends_at if current else None,
        )

    def _subscription_select(self) -> Select[Any]:
        return select(SubscriptionModel, SubscriptionPlanModel).join(
            SubscriptionPlanModel, SubscriptionPlanModel.id == SubscriptionModel.plan_id
        )

    # ------------------------------------------------------------------ #
    # Служебное                                                           #
    # ------------------------------------------------------------------ #

    async def set_status(self, subscriber_id: UUID, status: str | UserStatus) -> User:
        resolved = str(getattr(status, "value", status))
        if resolved not in {item.value for item in UserStatus}:
            raise ValueError(f"неизвестный статус подписчика: {resolved}")
        async with self._session_factory() as session, session.begin():
            model = await session.get(SubscriberModel, subscriber_id, with_for_update=True)
            if model is None:
                raise SubscriberNotFoundError(f"Подписчик {subscriber_id} не найден")
            model.status = resolved
            model.updated_at = datetime.now(UTC)
            if resolved in {UserStatus.BLOCKED.value, UserStatus.LEFT.value}:
                await session.execute(
                    update(SubscriberProfileModel)
                    .where(SubscriberProfileModel.subscriber_id == subscriber_id)
                    .values(digest_enabled=False)
                )
            await session.flush()
            user = _to_user(model)
        return user

    async def approve(self, subscriber_id: UUID, *, actor: str | None = None) -> User:
        """Впустить подписчика: статус active и несмываемая отметка в meta.

        Отметка нужна отдельно от статуса: статус меняется и дальше — чат
        уходит в `left`, человека ставят на паузу, — а вопрос «этого вообще
        когда-нибудь пропускали» после этого не должен теряться.
        """

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            model = await session.get(SubscriberModel, subscriber_id, with_for_update=True)
            if model is None:
                raise SubscriberNotFoundError(f"Подписчик {subscriber_id} не найден")
            model.status = UserStatus.ACTIVE.value
            # meta — JSONB: изменение словаря на месте SQLAlchemy не заметит,
            # поэтому кладём новый.
            model.meta = {
                **(model.meta or {}),
                "approved": True,
                "approved_at": now.isoformat(),
                "approved_by": actor,
            }
            # Подтверждение чата обязано включить плановый дайджест, иначе оно
            # ничего не меняет: диспетчер отбирает по `digest_enabled`, и
            # профиль, заведённый выключенным (или выключенный отказом ранее),
            # молча не получает ничего. Снаружи это выглядит как «подтвердили,
            # а бот не пишет» — и отладить это по статусу невозможно.
            #
            # Личке дайджест не включаем: в групп-центричной схеме материалы
            # туда не приходят вовсе, и включённый флаг означал бы рассылку,
            # которой не должно быть.
            if model.kind in CHAT_KINDS:
                await session.execute(
                    update(SubscriberProfileModel)
                    .where(SubscriberProfileModel.subscriber_id == subscriber_id)
                    .values(digest_enabled=True)
                )
            model.updated_at = now
            session.add(
                SubscriberActivityModel(
                    subscriber_id=subscriber_id,
                    kind="subscription_changed",
                    payload={"event": "approved", "actor": actor},
                )
            )
            if model.kind in CHAT_KINDS:
                session.add(
                    ChatEventModel(
                        subscriber_id=subscriber_id,
                        event_type="approved",
                        new_value={"status": UserStatus.ACTIVE.value, "actor": actor},
                    )
                )
            await session.flush()
            user = _to_user(model)
        return user

    async def reject(
        self, subscriber_id: UUID, *, actor: str | None = None, reason: str | None = None
    ) -> User:
        """Отказать в доступе: статус blocked и выключенные дайджесты."""

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            model = await session.get(SubscriberModel, subscriber_id, with_for_update=True)
            if model is None:
                raise SubscriberNotFoundError(f"Подписчик {subscriber_id} не найден")
            model.status = UserStatus.BLOCKED.value
            model.meta = {
                **(model.meta or {}),
                "approved": False,
                "rejected_at": now.isoformat(),
                "rejected_by": actor,
                "rejected_reason": _clean(reason),
            }
            model.updated_at = now
            await session.execute(
                update(SubscriberProfileModel)
                .where(SubscriberProfileModel.subscriber_id == subscriber_id)
                .values(digest_enabled=False)
            )
            session.add(
                SubscriberActivityModel(
                    subscriber_id=subscriber_id,
                    kind="subscription_changed",
                    payload={"event": "rejected", "actor": actor, "reason": _clean(reason)},
                )
            )
            if model.kind in CHAT_KINDS:
                # Симметрично approve: без этой записи в истории чата видно
                # только подтверждения, и отказ выглядит как «ничего не было».
                session.add(
                    ChatEventModel(
                        subscriber_id=subscriber_id,
                        event_type="rejected",
                        new_value={
                            "status": UserStatus.BLOCKED.value,
                            "actor": actor,
                            "reason": _clean(reason),
                        },
                    )
                )
            await session.flush()
            user = _to_user(model)
        return user

    async def pending_queue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Заявки, ждущие решения: люди и чаты одним списком.

        Одним запросом, а не двумя: очередь показывается на одном экране, и
        сортировать её нужно по времени появления целиком, иначе свежая группа
        уезжает под старые личные заявки.
        """

        statement = (
            select(SubscriberModel, ChatMembershipModel)
            .join(
                ChatMembershipModel,
                ChatMembershipModel.subscriber_id == SubscriberModel.id,
                isouter=True,
            )
            .where(SubscriberModel.status == UserStatus.PENDING.value)
            .order_by(SubscriberModel.first_seen_at.desc(), SubscriberModel.id)
            .limit(max(1, min(limit, 500)))
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        queue: list[dict[str, Any]] = []
        for subscriber, membership in rows:
            queue.append(
                {
                    "id": str(subscriber.id),
                    "kind": subscriber.kind,
                    "telegram_chat_id": subscriber.telegram_chat_id,
                    "title": subscriber.title,
                    "username": subscriber.telegram_username,
                    "first_seen_at": subscriber.first_seen_at,
                    "bot_status": membership.bot_status if membership else None,
                    "member_count": membership.member_count if membership else None,
                    "can_post_messages": membership.can_post_messages if membership else None,
                    "added_by_user_id": membership.added_by_user_id if membership else None,
                }
            )
        return queue

    async def record_activity(
        self,
        subscriber_id: UUID,
        kind: str,
        *,
        profile_id: UUID | None = None,
        item_id: UUID | None = None,
        digest_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            session.add(
                SubscriberActivityModel(
                    subscriber_id=subscriber_id,
                    profile_id=profile_id,
                    item_id=item_id,
                    digest_id=digest_id,
                    kind=kind,
                    payload=dict(payload or {}),
                )
            )

    async def forget(self, subscriber_id: UUID) -> None:
        """Полностью удалить подписчика — каскады уносят профили и историю."""

        async with self._session_factory() as session, session.begin():
            await session.execute(
                delete(SubscriberModel).where(SubscriberModel.id == subscriber_id)
            )


def is_approved(model: SubscriberModel) -> bool:
    """Подтверждал ли администратор этого подписчика хоть раз."""

    return bool((model.meta or {}).get("approved"))


def _status_after_return(model: SubscriberModel) -> str:
    """Каким статусом встречать чат, в который бота вернули.

    Одобренный однажды чат не должен вставать в очередь во второй раз: бота
    выкидывают и добавляют обратно по бытовым причинам — переезд в супергруппу,
    чистка администраторов, — и требовать подтверждения на каждый такой случай
    значит превратить модерацию в шум.
    """

    return UserStatus.ACTIVE.value if is_approved(model) else UserStatus.PENDING.value


def _to_user(model: SubscriberModel) -> User:
    return User(
        id=model.id,
        kind=SubscriberKind(model.kind),
        telegram_chat_id=model.telegram_chat_id,
        telegram_user_id=model.telegram_user_id,
        telegram_username=model.telegram_username,
        title=model.title,
        language_code=model.language_code,
        status=UserStatus(model.status),
        is_owner=model.is_owner,
        timezone=model.timezone,
        last_seen_at=model.last_seen_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _to_chat(row: Any) -> ChatRecord:
    subscriber: SubscriberModel = row[0]
    membership: ChatMembershipModel | None = row[1]
    return ChatRecord(
        subscriber_id=subscriber.id,
        kind=subscriber.kind,
        telegram_chat_id=subscriber.telegram_chat_id,
        title=subscriber.title,
        username=subscriber.telegram_username,
        status=subscriber.status,
        bot_status=membership.bot_status if membership else "left",
        chat_type=membership.chat_type if membership else None,
        member_count=membership.member_count if membership else None,
        can_post_messages=membership.can_post_messages if membership else None,
        added_by_user_id=membership.added_by_user_id if membership else None,
        added_at=membership.added_at if membership else None,
        removed_at=membership.removed_at if membership else None,
        last_checked_at=membership.last_checked_at if membership else None,
        error=membership.error if membership else None,
        profiles=int(row[2] or 0) if len(row) > 2 else 0,
    )


def _to_subscription(row: Any) -> SubscriptionRecord:
    subscription: SubscriptionModel = row[0]
    plan: SubscriptionPlanModel = row[1]
    return SubscriptionRecord(
        id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        plan_id=plan.id,
        plan_key=plan.key,
        plan_name=plan.name,
        status=subscription.status,
        starts_at=subscription.starts_at,
        ends_at=subscription.ends_at,
        grace_until=subscription.grace_until,
        auto_renew=subscription.auto_renew,
        source=subscription.source,
        notes=subscription.notes,
        created_at=subscription.created_at,
    )


__all__ = [
    "ABSENT_BOT_STATUSES",
    "ACTIVE_SUBSCRIPTION_STATUSES",
    "CHAT_KINDS",
    "PERSONAL_KINDS",
    "PRESENT_BOT_STATUSES",
    "ChatIdentity",
    "ChatRecord",
    "KindBreakdown",
    "PlanLimits",
    "PlanNotFoundError",
    "SubscriberNotFoundError",
    "SubscriberRepository",
    "SubscriptionNotFoundError",
    "SubscriptionOverlapError",
    "SubscriptionRecord",
    "is_approved",
    "kind_from_chat_type",
]
