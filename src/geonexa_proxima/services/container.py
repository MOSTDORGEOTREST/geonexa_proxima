"""Dependency container and conservative infrastructure bootstrap."""

from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from geonexa_proxima.config import Settings, get_settings
from geonexa_proxima.ports import (
    Analyzer,
    Collector,
    Embedder,
    ItemRepository,
    ProfileExplainer,
    ProfileVectorStore,
    Ranker,
    Reranker,
    UserProfileRepository,
    VectorStore,
)
from geonexa_proxima.services.digest import DigestBuilder
from geonexa_proxima.services.feedback import ProfileFeedbackService
from geonexa_proxima.services.ingestion import IngestionService
from geonexa_proxima.services.personalization import PersonalizationService
from geonexa_proxima.services.profiles import UserProfileService
from geonexa_proxima.services.search import SearchService
from geonexa_proxima.services.taxonomy import load_taxonomy

_FACTORIES = {
    "repository": ("geonexa_proxima.db", "create_repository"),
    "collectors": ("geonexa_proxima.collectors", "create_collectors"),
    "embedder": ("geonexa_proxima.ml", "create_embedder"),
    "reranker": ("geonexa_proxima.ml", "create_reranker"),
    "vector_store": ("geonexa_proxima.vector", "create_vector_store"),
    "profile_vector_store": ("geonexa_proxima.vector", "create_profile_vector_store"),
    "ranker": ("geonexa_proxima.llm", "create_ranker"),
    "analyzer": ("geonexa_proxima.llm", "create_analyzer"),
}


@dataclass(slots=True)
class Container:
    settings: Settings
    repository: ItemRepository | None = None
    profile_repository: UserProfileRepository | None = None
    subscriber_repository: object | None = None
    engine: Any | None = None
    session_factory: Any | None = None
    collectors: Sequence[Collector] = field(default_factory=tuple)
    embedder: Embedder | None = None
    reranker: Reranker | None = None
    vector_store: VectorStore | None = None
    profile_vector_store: ProfileVectorStore | None = None
    matcher: object | None = None
    decision_sink: object | None = None
    ranker: Ranker | None = None
    analyzer: Analyzer | None = None
    personalizer: ProfileExplainer | None = None
    deep_personalizer: ProfileExplainer | None = None
    profile_text: str = ""
    resources: Sequence[object] = field(default_factory=tuple, repr=False)
    _bot: Any | None = field(default=None, repr=False, compare=False)

    def readiness(self) -> dict[str, bool]:
        return {
            "repository": self.repository is not None,
            "profile_repository": self.profile_repository is not None,
            "collectors": bool(self.collectors),
            "embedder": self.embedder is not None,
            "vector_store": self.vector_store is not None,
            "profile_vector_store": self.profile_vector_store is not None,
            "ranker": self.ranker is not None,
            "analyzer": self.analyzer is not None,
        }

    @property
    def ready(self) -> bool:
        return all(self.readiness().values())

    def require_engine(self) -> Any:
        """Движок нужен флоу напрямую: очередь доставки и роллапы ходят в SQL."""

        if self.engine is None:
            raise RuntimeError("Движок БД не сконфигурирован: bootstrap должен вернуть engine")
        return self.engine

    def telegram_bot(self) -> Any:
        """Один экземпляр Bot на контейнер.

        Воркер рассылки шлёт сотни сообщений подряд: каждый новый Bot — это
        новая HTTP-сессия к api.telegram.org, а лимиты считаются на токен, а не
        на сессию. Держим один и закрываем его в ``close``.
        """

        if self._bot is None:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode

            self._bot = Bot(
                token=self.settings.telegram_bot_token.get_secret_value(),
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        return self._bot

    def subscribers(self) -> Any:
        """Kind-aware репозиторий: подписчики, чаты, подписки."""

        if self.subscriber_repository is None:
            raise RuntimeError("Репозиторий подписчиков не сконфигурирован")
        return self.subscriber_repository

    def ingestion_service(
        self, *, run_id: Any | None = None, logger: Any | None = None
    ) -> IngestionService:
        """Собрать пайплайн сбора.

        ``run_id`` включает журнал решений: строки в `harvest_decisions`
        обязаны ссылаться на прогон, поэтому вне прогона журнал не пишется.

        ``logger`` — куда сервис рассказывает про источники. Флоу передаёт сюда
        логгер прогона Prefect: иначе построчный отчёт по источникам уходит в
        stdout контейнера, а в хвосте прогона его не видно.
        """

        required = {
            "repository": self.repository,
            "collectors": self.collectors,
            "embedder": self.embedder,
            "vector_store": self.vector_store,
            "ranker": self.ranker,
            "analyzer": self.analyzer,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Missing ingestion dependencies: " + ", ".join(missing))
        assert (
            self.repository
            and self.embedder
            and self.vector_store
            and self.ranker
            and self.analyzer
        )
        sink = self.decision_sink
        counter = None
        if self.engine is not None:
            from geonexa_proxima.services.decisions import PostgresDecisionSink, TermHitCounter

            counter = TermHitCounter(self.engine)
            if sink is None and run_id is not None:
                sink = PostgresDecisionSink(
                    self.engine,
                    run_id,
                    store_rejected=self.settings.harvest_store_rejected,
                )
        return IngestionService(
            collectors=self.collectors,
            repository=self.repository,
            embedder=self.embedder,
            vector_store=self.vector_store,
            ranker=self.ranker,
            analyzer=self.analyzer,
            reranker=self.reranker,
            profile_text=self.profile_text,
            matcher=self.matcher or self.build_matcher(),
            decision_sink=sink,
            semantic_threshold=self.settings.semantic_threshold,
            deep_analysis_threshold=self.settings.deep_analysis_threshold,
            embedding_batch_size=self.settings.embedding_batch_size,
            keyword_threshold=self.settings.harvest_keyword_threshold,
            store_rejected=self.settings.harvest_store_rejected,
            cursors=self.source_cursors(),
            term_counter=counter,
            logger=logger,
        )

    def source_cursors(self) -> Any | None:
        """Курсоры источников. Без движка сбор идёт по фиксированному окну."""

        if self.engine is None:
            return None
        from geonexa_proxima.services.cursors import SourceCursors

        return SourceCursors(self.engine, profile_key=self.settings.harvest_profile_key)

    def build_matcher(self) -> object | None:
        """Собрать гейт из YAML-профиля.

        Профиль может отсутствовать в тестовых окружениях — тогда пайплайн
        работает без гейта, как раньше, а не падает на старте.
        """

        path = self.settings.harvest_config_path
        if not path.is_file():
            return None
        from geonexa_proxima.harvest import HarvestMatcher, load_harvest_profile

        return HarvestMatcher(load_harvest_profile(path))

    def digest_builder(self) -> DigestBuilder:
        if self.repository is None:
            raise RuntimeError("Репозиторий материалов не сконфигурирован: нет подключения к базе")
        return DigestBuilder(
            self.repository,
            personalization=self.personalization_service(),
        )

    def search_service(self) -> SearchService:
        if self.repository is None or self.embedder is None or self.vector_store is None:
            raise RuntimeError(
                "Поиску нужны репозиторий, эмбеддер и векторное хранилище — "
                "проверь EMBEDDING_MODE и VECTOR_BACKEND в /ready"
            )
        return SearchService(
            repository=self.repository,
            embedder=self.embedder,
            vector_store=self.vector_store,
            reranker=self.reranker,
        )

    def profile_service(self) -> UserProfileService:
        if self.profile_repository is None:
            raise RuntimeError("Репозиторий профилей не сконфигурирован")
        return UserProfileService(self.profile_repository, self.profile_text)

    def personalization_service(self) -> PersonalizationService:
        if not all(
            (
                self.repository,
                self.profile_repository,
                self.embedder,
                self.vector_store,
                self.profile_vector_store,
            )
        ):
            raise RuntimeError(
                "Персонализация не собрана: нужны репозитории, эмбеддер и оба "
                "векторных хранилища. Что именно отсутствует, показывает /ready"
            )
        assert self.repository
        assert self.profile_repository
        assert self.embedder
        assert self.vector_store
        assert self.profile_vector_store
        return PersonalizationService(
            settings=self.settings,
            item_repository=self.repository,
            profile_repository=self.profile_repository,
            embedder=self.embedder,
            item_vectors=self.vector_store,
            profile_vectors=self.profile_vector_store,
            reranker=self.reranker,
            explainer=self.personalizer,
        )

    def feedback_service(self) -> ProfileFeedbackService:
        if self.repository is None or self.profile_repository is None:
            raise RuntimeError("Обработка обратной связи не сконфигурирована")
        return ProfileFeedbackService(
            item_repository=self.repository,
            profile_repository=self.profile_repository,
            profile_service=self.profile_service(),
        )

    async def close(self) -> None:
        """Закрыть зависимости с обычным хуком закрытия.

        Движок сюда не входит намеренно: он один на процесс (см. ``get_engine``),
        и восемь параллельных флоу подписчиков закрыли бы общий пул друг под
        другом. Пул гасится один раз на остановке сервиса — ``dispose_engines``.
        """

        seen: set[int] = set()
        if self._bot is not None:
            bot, self._bot = self._bot, None
            await bot.session.close()
        values: list[object] = [
            *self.collectors,
            self.analyzer,
            self.ranker,
            self.reranker,
            self.vector_store,
            self.profile_vector_store,
            self.embedder,
            self.profile_repository,
            self.repository,
            *self.resources,
        ]
        for value in values:
            if value is None or id(value) in seen:
                continue
            seen.add(id(value))
            closer = (
                getattr(value, "aclose", None)
                or getattr(value, "close", None)
                or getattr(value, "dispose", None)
            )
            if closer is None:
                continue
            result = closer()
            if inspect.isawaitable(result):
                await result


def load_container(
    settings: Settings | None = None,
    *,
    target: str | None = None,
    require_ready: bool = True,
) -> Container:
    """Build dependencies via an explicit bootstrap or conventional factory functions.

    ``GEONEXA_BOOTSTRAP=package.module:callable`` is the stable integration seam.
    The callable may accept ``settings`` and return either a Container or a mapping
    keyed by the Container field names.
    """

    settings = settings or get_settings()
    target = target or os.getenv("GEONEXA_BOOTSTRAP")
    if target:
        built = _call_factory(*_split_target(target), settings=settings)
        container = _coerce_container(built, settings)
    else:
        components: dict[str, Any] = {}
        taxonomy = (
            load_taxonomy(settings.taxonomy_path) if settings.taxonomy_path.is_file() else None
        )
        llm_bundle_factory = _find_factory("geonexa_proxima.llm", "create_llm_providers")
        if llm_bundle_factory is not None:
            llm_bundle = _invoke(llm_bundle_factory, settings)
            components["ranker"] = llm_bundle.ranker
            components["analyzer"] = llm_bundle.analyzer
            components["personalizer"] = llm_bundle.personalizer
            components["deep_personalizer"] = llm_bundle.deep_personalizer
            components["resources"] = [llm_bundle]
        for name, (module_name, factory_name) in _FACTORIES.items():
            if name in components:
                continue
            factory = _find_factory(module_name, factory_name)
            if factory is not None:
                extras = (
                    {"taxonomy": taxonomy.queries()}
                    if name == "collectors" and taxonomy is not None
                    else {}
                )
                components[name] = _invoke(factory, settings, **extras)
        if "repository" not in components:
            repository_parts = _build_conventional_repositories(settings)
            if repository_parts is not None:
                components.update(repository_parts)
        container = _coerce_container(components, settings)
    if require_ready and not container.ready:
        missing = [name for name, ready in container.readiness().items() if not ready]
        raise RuntimeError(
            "Application infrastructure is incomplete ("
            + ", ".join(missing)
            + "). Set GEONEXA_BOOTSTRAP=module:factory or expose conventional create_* factories."
        )
    return container


def _coerce_container(value: object, settings: Settings) -> Container:
    if isinstance(value, Container):
        return value
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("Bootstrap must return Container or a component mapping")
    components = dict(value)
    profile_text = str(components.pop("profile_text", "")).strip()
    if not profile_text and settings.taxonomy_path.is_file():
        profile_text = load_taxonomy(settings.taxonomy_path).profile_text
    allowed = {
        "repository",
        "profile_repository",
        "subscriber_repository",
        "engine",
        "session_factory",
        "collectors",
        "embedder",
        "reranker",
        "vector_store",
        "profile_vector_store",
        "ranker",
        "analyzer",
        "personalizer",
        "deep_personalizer",
        "resources",
    }
    unknown = set(components) - allowed
    if unknown:
        raise ValueError("Unknown bootstrap components: " + ", ".join(sorted(unknown)))
    return Container(settings=settings, profile_text=profile_text, **components)


def _find_factory(module_name: str, factory_name: str) -> Callable[..., object] | None:
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        if exc.name == module_name:
            return None
        raise
    factory = getattr(module, factory_name, None)
    return factory if callable(factory) else None


def _split_target(target: str) -> tuple[str, str]:
    module_name, separator, name = target.partition(":")
    if not separator or not module_name or not name:
        raise ValueError("Bootstrap target must have the form 'package.module:callable'")
    return module_name, name


def _call_factory(module_name: str, name: str, *, settings: Settings) -> object:
    module = importlib.import_module(module_name)
    factory = getattr(module, name)
    if not callable(factory):
        raise TypeError(f"Bootstrap target is not callable: {module_name}:{name}")
    return _invoke(factory, settings)


def _invoke(factory: Callable[..., object], settings: Settings, **extras: object) -> object:
    signature = inspect.signature(factory)
    if not signature.parameters:
        return factory()
    accepted_extras = {
        name: value for name, value in extras.items() if name in signature.parameters
    }
    if "settings" in signature.parameters:
        return factory(settings=settings, **accepted_extras)
    return factory(settings, **accepted_extras)


def _build_conventional_repositories(
    settings: Settings,
) -> dict[str, Any] | None:
    """Compose the confirmed db package primitives when no repository factory exists."""

    try:
        module = importlib.import_module("geonexa_proxima.db")
    except ImportError as exc:
        if exc.name == "geonexa_proxima.db":
            return None
        raise
    create_session_factory = getattr(module, "create_session_factory", None)
    repository_type = getattr(module, "SQLAlchemyItemRepository", None)
    profile_repository_type = getattr(module, "SQLAlchemyUserProfileRepository", None)
    subscriber_repository_type = getattr(module, "SubscriberRepository", None)
    if not all(
        callable(value)
        for value in (
            create_session_factory,
            repository_type,
            profile_repository_type,
            subscriber_repository_type,
        )
    ):
        return None
    from geonexa_proxima.db.session import get_engine

    engine = get_engine(settings, application_name=settings.db_application_name)
    session_factory = create_session_factory(engine)
    return {
        "repository": repository_type(session_factory),
        "profile_repository": profile_repository_type(session_factory),
        "subscriber_repository": subscriber_repository_type(session_factory),
        "engine": engine,
        "session_factory": session_factory,
    }
