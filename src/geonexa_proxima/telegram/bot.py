"""Aiogram 3 bot assembled from application services."""

from __future__ import annotations

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
    InlineKeyboardButton,
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
from geonexa_proxima.telegram.middleware import AllowedUserMiddleware


@dataclass(slots=True)
class TelegramApplication:
    bot: Bot
    dispatcher: Dispatcher
    container: Container

    async def set_commands(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="daily", description="Daily digest"),
                BotCommand(command="week", description="Weekly digest"),
                BotCommand(command="hot", description="Highest-scoring items"),
                BotCommand(command="papers", description="Research papers"),
                BotCommand(command="tools", description="Software and repositories"),
                BotCommand(command="datasets", description="Datasets"),
                BotCommand(command="search", description="Semantic search"),
                BotCommand(command="trends", description="Trending research topics"),
                BotCommand(command="why", description="Explain an item's relevance"),
                BotCommand(command="profiles", description="List research profiles"),
                BotCommand(command="profile_new", description="Create a profile"),
                BotCommand(command="profile_use", description="Activate a profile"),
                BotCommand(command="profile_edit", description="Edit active profile"),
                BotCommand(command="profile_delete", description="Delete a profile"),
                BotCommand(command="interests", description="Manage profile interests"),
                BotCommand(command="personalization", description="Profile status and digest"),
            ]
        )


class CreateProfileForm(StatesGroup):
    name = State()
    description = State()


class EditProfileForm(StatesGroup):
    description = State()


_FEEDBACK_CODES = {
    "vi": FeedbackKind.VERY_INTERESTING,
    "u": FeedbackKind.USEFUL,
    "ni": FeedbackKind.NOT_INTERESTING,
    "s": FeedbackKind.SAVE,
    "d": FeedbackKind.DEEPER,
}


def _item_keyboard(profile_score_id: UUID) -> InlineKeyboardMarkup:
    suffix = str(profile_score_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Very interesting",
                    callback_data=f"fb:vi:{suffix}",
                ),
                InlineKeyboardButton(text="Useful", callback_data=f"fb:u:{suffix}"),
            ],
            [
                InlineKeyboardButton(
                    text="Not for me",
                    callback_data=f"fb:ni:{suffix}",
                ),
                InlineKeyboardButton(text="Save", callback_data=f"fb:s:{suffix}"),
                InlineKeyboardButton(text="Go deeper", callback_data=f"fb:d:{suffix}"),
            ],
            [InlineKeyboardButton(text="Why this?", callback_data=f"pw:{suffix}")],
        ]
    )


async def _registered(message: Message, container: Container) -> tuple[User, UserProfile]:
    telegram_user = message.from_user
    if telegram_user is None:
        raise RuntimeError("Telegram update has no user")
    return await container.profile_service().register_user(
        telegram_user.id,
        username=telegram_user.username,
        display_name=telegram_user.full_name,
        language_code=telegram_user.language_code,
    )


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
        await message.answer("No matching items yet.")
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
    authorization = AllowedUserMiddleware(set(settings.telegram_allowed_user_ids))
    router.message.outer_middleware(authorization)
    router.callback_query.outer_middleware(authorization)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        _, profile = await _registered(message, container)
        await message.answer(
            "<b>GeoNexa Proxima</b>\n"
            "Your private radar for geotechnical research, AI methods, tools, and datasets.\n\n"
            f"Active profile: <b>{escape(profile.name)}</b>\n"
            "Use /profile_edit to describe your interests, for example: "
            "«I am a geotechnical engineer working on ML for soil liquefaction».\n\n"
            "Use /daily, /week, /hot, /papers, /tools, /datasets, or /search &lt;query&gt;."
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Current profile operation cancelled.")

    @router.message(Command("profiles"))
    async def profiles(message: Message) -> None:
        user, _ = await _registered(message, container)
        user_profiles = await container.profile_service().list_profiles(user.id)
        lines = ["<b>Your research profiles</b>"]
        for profile in user_profiles:
            flags = []
            if profile.is_active:
                flags.append("active")
            if profile.digest_enabled:
                flags.append("digest on")
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
            await message.answer("Describe what this profile should track. Send /skip for empty.")
            return
        await state.set_state(CreateProfileForm.name)
        await message.answer("Send a short unique profile name. Use /cancel to stop.")

    @router.message(CreateProfileForm.name)
    async def profile_new_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not name:
            await message.answer("Profile name cannot be empty.")
            return
        await state.update_data(profile_name=name)
        await state.set_state(CreateProfileForm.description)
        await message.answer("Describe your interests in natural language. Send /skip for empty.")

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
        await message.answer(f"Profile <b>{escape(profile.name)}</b> created and activated.")

    @router.message(Command("profile_use"))
    async def profile_use(message: Message) -> None:
        user, _ = await _registered(message, container)
        selector = (message.text or "").partition(" ")[2].strip()
        profile = await _resolve_profile(container, user.id, selector) if selector else None
        if profile is None:
            await message.answer("Usage: /profile_use &lt;name or UUID&gt;")
            return
        activated = await container.profile_service().activate_profile(user.id, profile.id)
        await message.answer(f"Active profile: <b>{escape(activated.name)}</b>")

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
                f"Profile <b>{escape(updated.name)}</b> updated (version {updated.version})."
            )
            return
        await state.set_state(EditProfileForm.description)
        await message.answer(
            "Describe what you want this profile to track. "
            "The current description will be replaced."
        )

    @router.message(EditProfileForm.description)
    async def profile_edit_description(message: Message, state: FSMContext) -> None:
        _, active = await _registered(message, container)
        description = (message.text or "").strip()
        if not description:
            await message.answer("Description cannot be empty; use /cancel.")
            return
        updated = await container.profile_service().update_profile(
            active.user_id,
            active.id,
            description=description,
        )
        await state.clear()
        await message.answer(
            f"Profile <b>{escape(updated.name)}</b> updated (version {updated.version})."
        )

    @router.message(Command("profile_delete"))
    async def profile_delete(message: Message) -> None:
        user, _ = await _registered(message, container)
        selector = (message.text or "").partition(" ")[2].strip()
        profile = await _resolve_profile(container, user.id, selector) if selector else None
        if profile is None:
            await message.answer("Usage: /profile_delete &lt;name or UUID&gt;")
            return
        try:
            active = await container.profile_service().delete_profile(user.id, profile.id)
        except ValueError as error:
            await message.answer(escape(str(error)))
            return
        await message.answer(f"Profile deleted. Active profile: <b>{escape(active.name)}</b>")

    @router.message(Command("interests"))
    async def interests(message: Message) -> None:
        user, profile = await _registered(message, container)
        arguments = (message.text or "").partition(" ")[2].strip()
        service = container.profile_service()
        if not arguments:
            values = await container.profile_repository.list_interests(user.id, profile.id)
            lines = [f"<b>Interests · {escape(profile.name)}</b>"]
            lines.extend(
                f"• {value.polarity.value} {value.weight:g}: {escape(value.target_text)} "
                f"<code>{value.id}</code>"
                for value in values
            )
            if len(lines) == 1:
                lines.append(
                    "No explicit interests. The natural-language description still applies."
                )
            lines.append(
                "\nAdd: <code>/interests add + 5 soil liquefaction</code>\n"
                "Remove: <code>/interests remove UUID</code>"
            )
            await message.answer("\n".join(lines))
            return
        parts = arguments.split(maxsplit=3)
        if len(parts) == 4 and parts[0] == "add" and parts[1] in {"+", "-"}:
            try:
                weight = float(parts[2])
            except ValueError:
                await message.answer("Weight must be a number from 0 to 10.")
                return
            polarity = InterestPolarity.POSITIVE if parts[1] == "+" else InterestPolarity.NEGATIVE
            interest = await service.add_interest(
                user.id,
                profile.id,
                query=parts[3],
                polarity=polarity,
                weight=weight,
            )
            await message.answer(f"Interest saved: {escape(interest.target_text)}")
            return
        if len(parts) == 2 and parts[0] == "remove":
            try:
                interest_id = UUID(parts[1])
            except ValueError:
                await message.answer("Interest ID must be a UUID.")
                return
            await service.remove_interest(user.id, profile.id, interest_id)
            await message.answer("Interest removed.")
            return
        await message.answer(
            "Usage:\n"
            "<code>/interests add + 5 soil liquefaction</code>\n"
            "<code>/interests add - 3 pavement crack vision</code>\n"
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
        description = profile.description or "Uses only the base GeoNexa taxonomy."
        await message.answer(
            f"<b>{escape(profile.name)}</b>\n"
            f"Version: {profile.version}\n"
            f"Scheduled digest: {'on' if profile.digest_enabled else 'off'}\n"
            f"Description: {escape(description)}\n\n"
            "Use <code>/personalization on</code> or <code>/personalization off</code>."
        )

    @router.message(Command("daily"))
    async def daily(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Daily GeoNexa digest",
            limit=20,
            minimum_score=settings.digest_score_threshold,
            since=datetime.now(UTC) - timedelta(days=1),
        )

    @router.message(Command("week"))
    async def week(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Weekly GeoNexa digest",
            limit=50,
            minimum_score=settings.digest_score_threshold,
            since=datetime.now(UTC) - timedelta(days=7),
        )

    @router.message(Command("hot"))
    async def hot(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Hot items",
            limit=20,
            minimum_score=settings.alert_score_threshold,
        )

    @router.message(Command("papers"))
    async def papers(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Papers",
            limit=30,
            minimum_score=settings.digest_score_threshold,
            kinds={ItemKind.PAPER, ItemKind.METHOD},
        )

    @router.message(Command("tools"))
    async def tools(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Tools",
            limit=30,
            minimum_score=settings.digest_score_threshold,
            kinds={ItemKind.SOFTWARE},
        )

    @router.message(Command("datasets"))
    async def datasets(message: Message) -> None:
        await _send_digest(
            message,
            container,
            heading="Datasets",
            limit=30,
            minimum_score=settings.digest_score_threshold,
            kinds={ItemKind.DATASET},
        )

    @router.message(Command("search"))
    async def search(message: Message) -> None:
        _, profile = await _registered(message, container)
        query = (message.text or "").partition(" ")[2].strip()
        if not query:
            await message.answer("Usage: /search &lt;query&gt;")
            return
        hits = await container.search_service().search(
            query,
            limit=10,
            profile_text=profile.compiled_text,
        )
        if not hits:
            await message.answer("No semantic matches found.")
            return
        lines = [f"<b>Search: {escape(query)}</b>"]
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
            await message.answer("Not enough ranked items to calculate trends yet.")
            return
        lines = ["<b>Current research trends</b>"]
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
            await message.answer("Usage: /why &lt;item UUID&gt;")
            return
        item = await container.repository.get(item_id)
        if item is None:
            await message.answer("Item not found.")
            return
        details = [f"<b>Why: {escape(item.title)}</b>"]
        scores = await container.profile_repository.list_profile_item_scores(
            user.id,
            profile.id,
            profile_version=profile.version,
            limit=settings.personalization_candidate_limit,
        )
        personal_score = next((score for score in scores if score.item_id == item_id), None)
        if personal_score:
            details.append(f"Personal score: {personal_score.personal_score * 10:.1f}/10")
            if personal_score.explanation:
                details.append(escape(personal_score.explanation))
        if container.deep_personalizer:
            try:
                deep_reason = await container.deep_personalizer.explain(
                    item,
                    profile_text=profile.compiled_text,
                    personal_score=(personal_score.personal_score if personal_score else 0),
                )
                details.append("<b>Profile-specific analysis</b>\n" + escape(deep_reason))
            except Exception:
                pass
        if item.rank:
            details.append(escape(item.rank.reason))
        if item.analysis:
            details.append("<b>For geotechnics</b>\n" + escape(item.analysis.geotechnical_transfer))
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
            await callback.answer("Invalid score reference.", show_alert=True)
            return
        score = await container.profile_repository.get_profile_item_score(user.id, score_id)
        if score is None:
            await callback.answer("This personalized result has expired.", show_alert=True)
            return
        item = await container.repository.get(score.item_id)
        if callback.message and item:
            profiles = await container.profile_service().list_profiles(user.id)
            source_profile = next(
                (profile for profile in profiles if profile.id == score.profile_id),
                None,
            )
            details = [
                f"<b>Why: {escape(item.title)}</b>",
                f"Personal score: {score.personal_score * 10:.1f}/10",
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
                    details.append("<b>Profile-specific analysis</b>\n" + escape(deep_reason))
                except Exception:
                    pass
            if item.analysis:
                details.append(
                    "<b>For geotechnics</b>\n" + escape(item.analysis.geotechnical_transfer)
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
            await callback.answer("Unknown feedback action.", show_alert=True)
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
            await callback.answer("This personalized result has expired.", show_alert=True)
            return
        await container.feedback_service().record(
            user_id=user.id,
            profile_id=score.profile_id,
            item_id=score.item_id,
            kind=kind,
            context={"transport": "telegram", "profile_score_id": str(score_id)},
        )
        await callback.answer(f"Marked as {kind.value.replace('_', ' ')}.")

    dispatcher.include_router(router)
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
