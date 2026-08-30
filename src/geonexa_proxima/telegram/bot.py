"""Бот на aiogram 3, собранный из сервисов приложения.

Весь текст, который видит человек, — на русском: продукт русскоязычный, и
английские подписи в интерфейсе были наследием первых прототипов.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    Message,
)

from geonexa_proxima.domain import (
    FeedbackKind,
    InterestPolarity,
    ItemKind,
    User,
    UserProfile,
)
from geonexa_proxima.services.container import Container, load_container
from geonexa_proxima.telegram.chats import register_chat_router
from geonexa_proxima.telegram.keyboards import FEEDBACK_CODES, feedback_keyboard
from geonexa_proxima.telegram.middleware import AccessMiddleware, AccessPolicy

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TelegramApplication:
    bot: Bot
    dispatcher: Dispatcher
    container: Container

    async def set_commands(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="daily", description="Дайджест за сутки"),
                BotCommand(command="week", description="Дайджест за неделю"),
                BotCommand(command="hot", description="Самое важное"),
                BotCommand(command="papers", description="Научные статьи"),
                BotCommand(command="tools", description="Софт и репозитории"),
                BotCommand(command="datasets", description="Наборы данных"),
                BotCommand(command="search", description="Смысловой поиск"),
                BotCommand(command="trends", description="Что сейчас на подъёме"),
                BotCommand(command="why", description="Почему это показано"),
                BotCommand(command="profiles", description="Мои профили"),
                BotCommand(command="profile_new", description="Создать профиль"),
                BotCommand(command="profile_use", description="Сделать профиль активным"),
                BotCommand(command="profile_edit", description="Изменить активный профиль"),
                BotCommand(command="profile_delete", description="Удалить профиль"),
                BotCommand(command="interests", description="Интересы профиля"),
                BotCommand(command="personalization", description="Состояние профиля и дайджест"),
            ]
        )


class CreateProfileForm(StatesGroup):
    name = State()
    description = State()


class EditProfileForm(StatesGroup):
    description = State()


#: Коды кнопок берём из общего модуля клавиатур: разметку под материалом
#: рисует и бот, и воркер доставки, и разъезд между ними означал бы, что
#: кнопка из планового дайджеста не обрабатывается вовсе.
_FEEDBACK_CODES = {code: FeedbackKind(value) for code, value in FEEDBACK_CODES.items()}

#: Как называется реакция в подтверждении. Значения enum — машинные
#: («very_interesting»), показывать их человеку нельзя.
_FEEDBACK_LABELS = {
    FeedbackKind.VERY_INTERESTING: "очень интересно",
    FeedbackKind.USEFUL: "полезно",
    FeedbackKind.NOT_INTERESTING: "не моё",
    FeedbackKind.SAVE: "сохранено",
    FeedbackKind.DEEPER: "разберу подробнее",
}


def _item_keyboard(profile_score_id: UUID) -> InlineKeyboardMarkup:
    """Та же клавиатура, что у планового дайджеста, в типах aiogram."""

    return InlineKeyboardMarkup.model_validate(feedback_keyboard(profile_score_id))


async def _registered(message: Message, container: Container) -> tuple[User, UserProfile]:
    telegram_user = message.from_user
    if telegram_user is None:
        raise RuntimeError("В апдейте Telegram нет пользователя")
    result = await container.profile_service().register_user(
        telegram_user.id,
        username=telegram_user.username,
        display_name=telegram_user.full_name,
        language_code=telegram_user.language_code,
    )
    await _log_activity(
        container,
        result[0],
        "command",
        payload={"text": (message.text or "")[:64]},
    )
    return result


async def _log_activity(
    container: Container,
    user: User,
    kind: str,
    *,
    payload: dict[str, object] | None = None,
    item_id: UUID | None = None,
) -> None:
    """Записать событие в ленту активности.

    На этой ленте стоят DAU/WAU/MAU и удержание когорт. Начать писать её позже
    запуска — значит навсегда остаться без статистики за первые недели: задним
    числом события не восстанавливаются.

    Ошибка записи не должна мешать ответу пользователю: статистика дешевле
    работающего бота.
    """

    if container.session_factory is None:
        return
    try:
        from geonexa_proxima.db.subscriber_repository import SubscriberRepository

        await SubscriberRepository(container.session_factory).record_activity(
            user.id, kind, item_id=item_id, payload=payload
        )
    except Exception as error:
        log.debug("Событие %s для %s не записано: %s", kind, user.id, error)


async def _ensure_trial(container: Container, user: User) -> object | None:
    """Выдать пробный период при первом знакомстве.

    Идемпотентно и молча: повторный /start — обычное дело, и падать на нём
    нельзя. `DEFAULT_TRIAL_DAYS=0` отключает пробный период целиком.
    """

    settings = container.settings
    if not settings.default_trial_days or container.session_factory is None:
        return None
    from geonexa_proxima.db.subscriber_repository import SubscriberRepository

    try:
        return await SubscriberRepository(container.session_factory).start_trial(
            user.id,
            plan_key=settings.default_subscription_plan,
            trial_days=settings.default_trial_days,
            grace_days=settings.subscription_grace_days,
        )
    except Exception as error:
        log.warning("Пробный период для %s не выдан: %s", user.id, error)
        return None


async def _resolve_profile(
    container: Container,
    user_id: UUID,
    selector: str,
) -> UserProfile | None:
    profiles = await container.profile_service().list_profiles(user_id)
    try:
        profile_id = UUID(selector)
    except ValueError:
        profile_id = None
    selector_key = selector.strip().casefold()
    return next(
        (
            profile
            for profile in profiles
            if profile.id == profile_id or profile.normalized_name == selector_key
        ),
        None,
    )


async def _send_digest(
    message: Message,
    container: Container,
    *,
    heading: str,
    limit: int,
    minimum_score: float,
    kinds: set[ItemKind] | None = None,
    since: datetime | None = None,
) -> None:
    _, profile = await _registered(message, container)
    builder = container.digest_builder()
    candidates = await builder.list_personalized(
        profile,
        limit=limit,
        kinds=kinds,
        since=since,
        minimum_global_score=minimum_score,
    )
    await message.answer(f"<b>{escape(heading)} · {escape(profile.name)}</b>")
    if not candidates:
        await message.answer("Пока нечего показать: подходящих материалов нет.")
        return
    for candidate in candidates:
        await message.answer(
            builder.formatter.format_personalized_item(candidate),
            reply_markup=_item_keyboard(candidate.profile_score_id),
        )


def create_telegram_app(container: Container) -> TelegramApplication:
    settings = container.settings
    bot = Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    router = Router(name="geonexa")
    policy = AccessPolicy(
        settings.telegram_registration_mode,
        allowed_user_ids=settings.telegram_allowed_user_ids,
        owner_ids=settings.telegram_owner_ids,
        allow_group_chats=settings.telegram_allow_group_chats,
    )
    if policy.is_effectively_open:
        log.warning(
            "Бот отвечает всем: режим %s, список разрешённых пуст. "
            "Задай TELEGRAM_ALLOWED_USER_IDS или TELEGRAM_OWNER_IDS.",
            policy.mode.value,
        )
    authorization = AccessMiddleware(policy)
    router.message.outer_middleware(authorization)
    router.callback_query.outer_middleware(authorization)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        user, profile = await _registered(message, container)
        trial = await _ensure_trial(container, user)
        greeting = (
            "<b>Проксима</b> — радар научных публикаций ГЕОНЕКСЫ.\n"
            "Инженерная геология, геотехника и то, что делает с ними ИИ.\n\n"
            f"Активный профиль: <b>{escape(profile.name)}</b>\n"
            "Опишите интересы через /profile_edit — например: «инженер-геотехник, "
            "занимаюсь ML для разжижения грунтов».\n\n"
            "Команды: /daily, /week, /hot, /papers, /tools, /datasets, /search &lt;запрос&gt;."
        )
        if trial is not None and trial.ends_at is not None:
            greeting += (
                f"\n\nПробный период до {trial.ends_at:%d.%m.%Y} — тариф "
                f"«{escape(trial.plan_name)}»."
            )
        await message.answer(greeting)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.")

    @router.message(Command("profiles"))
    async def profiles(message: Message) -> None:
        user, _ = await _registered(message, container)
        user_profiles = await container.profile_service().list_profiles(user.id)
        lines = ["<b>Ваши профили</b>"]
        for profile in user_profiles:
            flags = []
            if profile.is_active:
                flags.append("активный")
            if profile.digest_enabled:
                flags.append("дайджест включён")
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"• <b>{escape(profile.name)}</b>{suffix}\n<code>{profile.id}</code>")
        await message.answer("\n\n".join(lines))

    @router.message(Command("profile_new"))
    async def profile_new(message: Message, state: FSMContext) -> None:
        await _registered(message, container)
        supplied_name = (message.text or "").partition(" ")[2].strip()
        if supplied_name:
            await state.update_data(profile_name=supplied_name)
            await state.set_state(CreateProfileForm.description)
            await message.answer("Опишите, за чем должен следить профиль. /skip — оставить пустым.")
            return
        await state.set_state(CreateProfileForm.name)
        await message.answer("Пришлите короткое название профиля. /cancel — отменить.")

    @router.message(CreateProfileForm.name)
    async def profile_new_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name:
            await message.answer("Название не может быть пустым.")
            return
        await state.update_data(profile_name=name)
        await state.set_state(CreateProfileForm.description)
        await message.answer("Опишите интересы обычными словами. /skip — оставить пустым.")

    @router.message(CreateProfileForm.description)
    async def profile_new_description(message: Message, state: FSMContext) -> None:
        user, _ = await _registered(message, container)
        data = await state.get_data()
        description = (
            None if (message.text or "").strip() == "/skip" else (message.text or "").strip()
        )
        profile = await container.profile_service().create_profile(
            user.id,
            str(data["profile_name"]),
            description=description,
            is_active=True,
        )
        await state.clear()
        await message.answer(f"Профиль <b>{escape(profile.name)}</b> создан и активирован.")

    @router.message(Command("profile_use"))
    async def profile_use(message: Message) -> None:
        user, _ = await _registered(message, container)
        selector = (message.text or "").partition(" ")[2].strip()
        profile = await _resolve_profile(container, user.id, selector) if selector else None
        if profile is None:
            await message.answer("Как пользоваться: /profile_use &lt;название или UUID&gt;")
            return
        activated = await container.profile_service().activate_profile(user.id, profile.id)
        await message.answer(f"Активный профиль: <b>{escape(activated.name)}</b>")

    @router.message(Command("profile_edit"))
    async def profile_edit(message: Message, state: FSMContext) -> None:
        _, active = await _registered(message, container)
        description = (message.text or "").partition(" ")[2].strip()
        if description:
            updated = await container.profile_service().update_profile(
                active.user_id,
                active.id,
                description=description,
            )
            await message.answer(
                f"Профиль <b>{escape(updated.name)}</b> обновлён (версия {updated.version})."
            )
            return
        await state.set_state(EditProfileForm.description)
        await message.answer(
            "Опишите, за чем должен следить профиль. Текущее описание будет заменено целиком."
        )

    @router.message(EditProfileForm.description)
    async def profile_edit_description(message: Message, state: FSMContext) -> None:
        _, active = await _registered(message, container)
        description = (message.text or "").strip()
        if not description:
            await message.answer("Описание не может быть пустым. /cancel — отменить.")
            return
        updated = await container.profile_service().update_profile(
            active.user_id,
            active.id,
            description=description,
        )
        await state.clear()
        await message.answer(
            f"Профиль <b>{escape(updated.name)}</b> обновлён (версия {updated.version})."
        )

    @router.message(Command("profile_delete"))
    async def profile_delete(message: Message) -> None:
        user, _ = await _registered(message, container)
        selector = (message.text or "").partition(" ")[2].strip()
        profile = await _resolve_profile(container, user.id, selector) if selector else None
        if profile is None:
            await message.answer("Как пользоваться: /profile_delete &lt;название или UUID&gt;")
            return
        try:
            active = await container.profile_service().delete_profile(user.id, profile.id)
        except ValueError as error:
            await message.answer(escape(str(error)))
            return
        await message.answer(f"Профиль удалён. Активный профиль: <b>{escape(active.name)}</b>")

    @router.message(Command("interests"))
    async def interests(message: Message) -> None:
        user, profile = await _registered(message, container)
        arguments = (message.text or "").partition(" ")[2].strip()
        service = container.profile_service()
        if not arguments:
            values = await container.profile_repository.list_interests(user.id, profile.id)
            lines = [f"<b>Интересы · {escape(profile.name)}</b>"]
            lines.extend(
                f"• {value.polarity.value} {value.weight:g}: {escape(value.target_text)} "
                f"<code>{value.id}</code>"
                for value in values
            )
            if len(lines) == 1:
                lines.append("Явных интересов нет — работает только текстовое описание профиля.")
            lines.append(
                "\nДобавить: <code>/interests add + 5 разжижение грунтов</code>\n"
                "Убрать: <code>/interests remove UUID</code>"
            )
            await message.answer("\n".join(lines))
            return
        parts = arguments.split(maxsplit=3)
        if len(parts) == 4 and parts[0] == "add" and parts[1] in {"+", "-"}:
            try:
                weight = float(parts[2])
            except ValueError:
                await message.answer("Вес — число от 0 до 10.")
                return
            polarity = InterestPolarity.POSITIVE if parts[1] == "+" else InterestPolarity.NEGATIVE
            interest = await service.add_interest(
                user.id,
                profile.id,
                query=parts[3],
                polarity=polarity,
                weight=weight,
            )
            await message.answer(f"Интерес сохранён: {escape(interest.target_text)}")
            return
        if len(parts) == 2 and parts[0] == "remove":
            try:
                interest_id = UUID(parts[1])
            except ValueError:
                await message.answer("Идентификатор интереса — это UUID.")
                return
            await service.remove_interest(user.id, profile.id, interest_id)
            await message.answer("Интерес убран.")
            return
        await message.answer(
            "Как пользоваться:\n"
            "<code>/interests add + 5 разжижение грунтов</code>\n"
            "<code>/interests add - 3 распознавание трещин в асфальте</code>\n"
            "<code>/interests remove UUID</code>"
        )

    @router.message(Command("personalization"))
    async def personalization(message: Message) -> None:
        user, profile = await _registered(message, container)
        action = (message.text or "").partition(" ")[2].strip().casefold()
        if action in {"on", "off"}:
            profile = await container.profile_service().update_profile(
                user.id,
                profile.id,
                digest_enabled=action == "on",
            )
        description = profile.description or "Используется только базовая таксономия ГЕОНЕКСЫ."
        await message.answer(
            f"<b>{escape(profile.name)}</b>\n"
            f"Версия: {profile.version}\n"
            f"Плановый дайджест: {'включён' if profile.digest_enabled else 'выключен'}\n"
            f"Описание: {escape(description)}\n\n"
            "Переключить: <code>/personalization on</code> или "
            "<code>/personalization off</code>."
        )

    @router.message(Command("daily"))
    async def daily(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Дайджест за сутки",
            limit=20,
            minimum_score=settings.digest_score_threshold,
            since=datetime.now(UTC) - timedelta(days=1),
        )

    @router.message(Command("week"))
    async def week(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Дайджест за неделю",
            limit=50,
            minimum_score=settings.digest_score_threshold,
            since=datetime.now(UTC) - timedelta(days=7),
        )

    @router.message(Command("hot"))
    async def hot(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Самое важное",
            limit=20,
            minimum_score=settings.alert_score_threshold,
        )

    @router.message(Command("papers"))
    async def papers(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Статьи",
            limit=30,
            minimum_score=settings.digest_score_threshold,
            kinds={ItemKind.PAPER, ItemKind.METHOD},
        )

    @router.message(Command("tools"))
    async def tools(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Инструменты",
            limit=30,
            minimum_score=settings.digest_score_threshold,
            kinds={ItemKind.SOFTWARE},
        )

    @router.message(Command("datasets"))
    async def datasets(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Данные",
            limit=30,
            minimum_score=settings.digest_score_threshold,
            kinds={ItemKind.DATASET},
        )

    @router.message(Command("search"))
    async def search(message: Message) -> None:
        _, profile = await _registered(message, container)
        query = (message.text or "").partition(" ")[2].strip()
        if not query:
            await message.answer("Как пользоваться: /search &lt;запрос&gt;")
            return
        hits = await container.search_service().search(
            query,
            limit=10,
            profile_text=profile.compiled_text,
        )
        if not hits:
            await message.answer("По смыслу ничего не нашлось.")
            return
        lines = [f"<b>Поиск: {escape(query)}</b>"]
        for hit in hits:
            lines.append(
                f"<b>{escape(hit.title)}</b> · {hit.score:.3f}"
                + (f"\n{escape(hit.snippet[:400])}" if hit.snippet else "")
            )
        await message.answer("\n\n".join(lines))

    @router.message(Command("trends"))
    async def trends(message: Message) -> None:
        items = await container.repository.list_digest_candidates(0, 100)
        topics = Counter(
            category for item in items if item.rank for category in item.rank.categories
        )
        if not topics:
            await message.answer("Пока мало оценённых материалов, тренды считать не на чем.")
            return
        lines = ["<b>Что сейчас на подъёме</b>"]
        lines.extend(
            f"{index}. {escape(topic)} — {count}"
            for index, (topic, count) in enumerate(topics.most_common(10), start=1)
        )
        await message.answer("\n".join(lines))

    async def send_why(message: Message, raw_item_id: str) -> None:
        user, profile = await _registered(message, container)
        try:
            item_id = UUID(raw_item_id)
        except ValueError:
            await message.answer("Как пользоваться: /why &lt;UUID материала&gt;")
            return
        item = await container.repository.get(item_id)
        if item is None:
            await message.answer("Материал не найден.")
            return
        details = [f"<b>Почему: {escape(item.title)}</b>"]
        scores = await container.profile_repository.list_profile_item_scores(
            user.id,
            profile.id,
            profile_version=profile.version,
            limit=settings.personalization_candidate_limit,
        )
        personal_score = next((score for score in scores if score.item_id == item_id), None)
        if personal_score:
            details.append(f"Персональная оценка: {personal_score.personal_score * 10:.1f}/10")
            if personal_score.explanation:
                details.append(escape(personal_score.explanation))
        if container.deep_personalizer:
            try:
                deep_reason = await container.deep_personalizer.explain(
                    item,
                    profile_text=profile.compiled_text,
                    personal_score=(personal_score.personal_score if personal_score else 0),
                )
                details.append("<b>Разбор под ваш профиль</b>\n" + escape(deep_reason))
            except Exception as error:
                # Ответ важнее разбора: без LLM остальные блоки всё равно есть.
                # Но молчать нельзя — так неработающий ключ провайдера выглядел
                # бы как «модель просто ничего не сказала».
                log.warning("Глубокий разбор для %s не получен: %s", item_id, error)
        if item.rank:
            details.append(escape(item.rank.reason))
        if item.analysis:
            details.append(
                "<b>Что это даёт геотехнике</b>\n" + escape(item.analysis.geotechnical_transfer)
            )
        await message.answer("\n\n".join(details))

    @router.message(Command("why"))
    async def why_command(message: Message) -> None:
        await send_why(message, (message.text or "").partition(" ")[2].strip())

    @router.callback_query(F.data.startswith("pw:"))
    async def why_callback(callback: CallbackQuery) -> None:
        actor = callback.from_user
        user, _ = await container.profile_service().register_user(
            actor.id,
            username=actor.username,
            display_name=actor.full_name,
            language_code=actor.language_code,
        )
        try:
            score_id = UUID((callback.data or "").partition(":")[2])
        except ValueError:
            await callback.answer("Ссылка на оценку не распознана.", show_alert=True)
            return
        score = await container.profile_repository.get_profile_item_score(user.id, score_id)
        if score is None:
            await callback.answer("Этот результат уже устарел.", show_alert=True)
            return
        item = await container.repository.get(score.item_id)
        if callback.message and item:
            profiles = await container.profile_service().list_profiles(user.id)
            source_profile = next(
                (profile for profile in profiles if profile.id == score.profile_id),
                None,
            )
            details = [
                f"<b>Почему: {escape(item.title)}</b>",
                f"Персональная оценка: {score.personal_score * 10:.1f}/10",
            ]
            if score.explanation:
                details.append(escape(score.explanation))
            if container.deep_personalizer and source_profile:
                try:
                    deep_reason = await container.deep_personalizer.explain(
                        item,
                        profile_text=source_profile.compiled_text,
                        personal_score=score.personal_score,
                    )
                    details.append("<b>Разбор под ваш профиль</b>\n" + escape(deep_reason))
                except Exception as error:
                    log.warning("Глубокий разбор для %s не получен: %s", score.item_id, error)
            if item.analysis:
                details.append(
                    "<b>Что это даёт геотехнике</b>\n" + escape(item.analysis.geotechnical_transfer)
                )
            await callback.message.answer("\n\n".join(details))
        await callback.answer()

    @router.callback_query(F.data.startswith("fb:"))
    async def feedback(callback: CallbackQuery) -> None:
        try:
            _, action, raw_score_id = (callback.data or "").split(":", maxsplit=2)
            kind = _FEEDBACK_CODES[action]
            score_id = UUID(raw_score_id)
        except (KeyError, ValueError):
            await callback.answer("Неизвестная реакция.", show_alert=True)
            return
        actor = callback.from_user
        user, _ = await container.profile_service().register_user(
            actor.id,
            username=actor.username,
            display_name=actor.full_name,
            language_code=actor.language_code,
        )
        score = await container.profile_repository.get_profile_item_score(user.id, score_id)
        if score is None:
            await callback.answer("Этот результат уже устарел.", show_alert=True)
            return
        await container.feedback_service().record(
            user_id=user.id,
            profile_id=score.profile_id,
            item_id=score.item_id,
            kind=kind,
            context={"transport": "telegram", "profile_score_id": str(score_id)},
        )
        await _log_activity(
            container,
            user,
            "feedback",
            item_id=score.item_id,
            payload={"kind": kind.value},
        )
        await callback.answer(f"Отмечено: {_FEEDBACK_LABELS[kind]}.")

    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Ответить человеку, даже когда обработчик упал.

        Без этого исключение в хендлере уходит только в лог, а в чате не
        появляется ничего: команда выглядит «не работающей», и отличить
        сломанный сервис от пустой выдачи снаружи невозможно. Текст ошибки
        пользователю не показываем — там могут быть внутренние адреса и имена,
        — но говорим, что произошло и что это не его вина.
        """

        log.exception(
            "Обработчик упал на апдейте %s", event.update.update_id, exc_info=event.exception
        )
        message = event.update.message or (
            event.update.callback_query.message if event.update.callback_query else None
        )
        note = (
            "Не получилось выполнить команду — сервис сейчас не в порядке. "
            "Мы уже видим ошибку в логах; попробуйте позже."
        )
        try:
            if event.update.callback_query is not None:
                await event.update.callback_query.answer(note, show_alert=True)
            elif message is not None:
                await message.answer(note)
        except Exception:  # ответить не удалось — значит, недоступен и Bot API
            log.warning("Не удалось сообщить об ошибке в чат")
        # True: событие обработано, aiogram не должен ронять polling.
        return True

    dispatcher.include_router(router)
    # Роутер чатов идёт отдельным: апдейт my_chat_member приходит без
    # from_user в привычном смысле и не должен проходить через политику
    # доступа для команд — иначе бота добавят в группу, а мы не узнаем.
    dispatcher.include_router(register_chat_router(container))
    return TelegramApplication(bot=bot, dispatcher=dispatcher, container=container)


async def run_polling(*, bootstrap_target: str | None = None) -> None:
    container = load_container(target=bootstrap_target)
    application = create_telegram_app(container)
    try:
        await application.set_commands()
        await application.dispatcher.start_polling(application.bot)
    finally:
        await application.bot.session.close()
        await container.close()
