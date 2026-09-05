"""Глобальный сбор: один процесс на всю платформу.

Персонализации здесь нет и быть не должно — корпус общий, оценка глобальная.
Флоу только оркеструет: вся логика живёт в сервисах, поэтому её можно
протестировать без Prefect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from geonexa_proxima.services.container import load_container
from geonexa_proxima.services.harvest_window import day_range, parse_day, scheduled_windows
from geonexa_proxima.services.ingestion import IngestionStats

#: Предел на ручной диапазон суток. Плановый догон ограничен своим пределом,
#: а здесь защита от опечатки в дате (2020 вместо 2026 — это две тысячи
#: суток), а не от долгого прогона: год по суткам — осознанный выбор
#: администратора, и он задаётся из админки как «последние N суток».
MAX_MANUAL_DAYS = 400


@dataclass(frozen=True, slots=True)
class _Chunk:
    """Один проход конвейера: подпись для лога и границы окна."""

    label: str
    since: datetime
    until: datetime | None
    #: Дата суток в ISO — ею подсказывают команду добора, когда источник упал.
    #: У открытого окна суток нет, и подсказывать нечего.
    day_key: str = ""


class HarvestAlreadyRunning(RuntimeError):
    """Сбор уже идёт.

    Частичный уникальный индекс `uq_harvest_runs_running` держит инвариант «не
    больше одного прогона одновременно»: два параллельных сбора ходили бы в те
    же источники, дважды тратили токены и гонялись за одни и те же строки.
    Отдельный класс нужен, чтобы это состояние выглядело как внятная фраза, а
    не как двести строк `IntegrityError` в логе.
    """


#: Кэш выключен намеренно. Prefect по умолчанию хеширует аргументы задачи,
#: чтобы понять, можно ли переиспользовать результат. Наши задачи принимают
#: живой контейнер с движком БД и блокировками — он не сериализуется, и Prefect
#: пишет в лог трейсбек «Unable to create hash» на каждом прогоне. Кэшировать
#: тут всё равно нечего: обе задачи пишут в базу, их смысл — побочный эффект.
@task(name="reclaim-stale-harvest-runs", cache_policy=NO_CACHE)
async def reclaim_stale_runs(container: Any, stale_after_minutes: int) -> int:
    """Закрыть прогоны, которые никто не закрыл.

    Процесс сбора может умереть между открытием записи и её закрытием: упал
    воркер, убили контейнер, кончилась память. Запись остаётся в статусе
    `running` навсегда, а уникальный индекс не пускает следующий сбор — и
    система перестаёт собирать вообще, сообщая об этом неразборчивым
    `IntegrityError`. Поэтому перед каждым стартом подбираем брошенное.

    Порог с запасом к длительности живого сбора, но не больше: пока запись не
    подобрана, система не собирает вообще.
    """

    async with container.require_engine().begin() as connection:
        # Живой прогон отмечается после каждых суток (`heartbeat`): ручной
        # сбор за квартал идёт часами и иначе выглядел бы брошенным, а
        # ночной плановый запуск помечал бы его неудачным и открывал второй
        # параллельно — ровно то, от чего защищает уникальный индекс.
        result = await connection.execute(
            text(
                "UPDATE harvest_runs SET status = 'failed', finished_at = now(), "
                "error = coalesce(error, 'Прогон оборван: процесс не закрыл запись. "
                "Помечен неудачным автоматически, иначе сбор был бы заблокирован.') "
                "WHERE status = 'running' "
                "  AND coalesce(CAST(stats ->> 'heartbeat_at' AS timestamptz), started_at)"
                "      < now() - make_interval(mins => :minutes) "
                "RETURNING id"
            ),
            {"minutes": stale_after_minutes},
        )
        return len(result.fetchall())


@task(name="harvest-heartbeat", cache_policy=NO_CACHE)
async def heartbeat(container: Any, run_id: uuid.UUID, done: int, planned: int) -> None:
    """Отметить, что прогон жив и сколько суток уже пройдено.

    Пишется в `stats` той же строки: отдельная колонка потребовала бы
    миграции ради одного поля, а экран прогонов и так читает `stats`.
    """

    import json

    async with container.require_engine().begin() as connection:
        await connection.execute(
            text(
                "UPDATE harvest_runs SET stats = coalesce(stats, '{}'::jsonb)"
                " || CAST(:progress AS jsonb) WHERE id = :id"
            ),
            {
                "id": str(run_id),
                "progress": json.dumps(
                    {
                        "heartbeat_at": datetime.now(UTC).isoformat(),
                        "days_done": done,
                        "days_planned": planned,
                    }
                ),
            },
        )


@task(name="last-covered-until", cache_policy=NO_CACHE)
async def last_covered_until(container: Any) -> datetime | None:
    """До какого момента корпус уже собран.

    Верхняя граница последнего успешного прогона — единственный честный ответ
    на вопрос «с какого дня продолжать». Курсоры источников для этого не
    годятся: они у каждого источника свои и двигаются по дате публикации, а не
    по границе окна.
    """

    async with container.require_engine().connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT max(until) AS covered FROM harvest_runs WHERE status = 'succeeded'"
                    )
                )
            )
            .mappings()
            .first()
        )
    return (row or {}).get("covered")


@task(name="open-harvest-run", retries=0, cache_policy=NO_CACHE)
async def open_run(
    container: Any, trigger: str, since: datetime, until: datetime | None = None
) -> uuid.UUID:
    """Открыть прогон. Частичный unique index не даст запустить второй параллельно."""

    run_id = uuid.uuid4()
    engine = container.require_engine()
    try:
        async with engine.begin() as connection:
            inserted = await connection.execute(
                text(
                    "INSERT INTO harvest_runs (id, harvest_profile_id, trigger, status, since, "
                    "until, triggered_by) "
                    "SELECT :id, p.id, :trigger, 'running', :since, :until, :by "
                    "FROM harvest_profiles p WHERE p.is_active LIMIT 1"
                ),
                {
                    "id": str(run_id),
                    "trigger": trigger,
                    "since": since,
                    "until": until,
                    "by": f"flow:{trigger}",
                },
            )
            if inserted.rowcount != 1:
                # `INSERT ... SELECT ... WHERE p.is_active` вставляет ноль
                # строк, если активного профиля сбора нет. Раньше функция
                # всё равно возвращала id, и сбор шёл дальше — а падал уже
                # первый сброс журнала решений, с невнятным нарушением
                # внешнего ключа посреди прогона.
                raise RuntimeError(
                    "Нет активного профиля сбора: прогон открывать не на что. "
                    "Проверьте таблицу harvest_profiles и HARVEST_PROFILE_KEY."
                )
    except IntegrityError as error:
        # Транзакция выше уже откачена, поэтому за подробностями идём заново.
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, started_at, triggered_by FROM harvest_runs "
                            "WHERE status = 'running' ORDER BY started_at LIMIT 1"
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise
        raise HarvestAlreadyRunning(
            f"Сбор уже идёт: прогон {row['id']} начат "
            f"{row['started_at']:%d.%m.%Y %H:%M} ({row['triggered_by'] or 'источник неизвестен'}). "
            f"Дождитесь его окончания или отмените в разделе «Прогоны»."
        ) from error
    return run_id


@task(name="close-harvest-run", cache_policy=NO_CACHE)
async def close_run(
    container: Any, run_id: uuid.UUID, status: str, stats: dict[str, Any], error: str | None
) -> None:
    import json

    async with container.require_engine().begin() as connection:
        await connection.execute(
            text(
                "UPDATE harvest_runs SET status=:status, finished_at=now(), "
                "stats=CAST(:stats AS jsonb), error=:error WHERE id=:id"
            ),
            {
                "id": str(run_id),
                "status": status,
                "stats": json.dumps(stats, ensure_ascii=False, default=str),
                "error": (error or "")[:4000] or None,
            },
        )


@flow(name="geonexa-global-harvest", log_prints=True)
async def global_harvest_flow(
    *,
    bootstrap_target: str | None = None,
    trigger: str = "schedule",
    day: str | None = None,
    since_day: str | None = None,
    until_day: str | None = None,
    days_back: int | None = None,
    max_catchup_days: int | None = None,
    limit_per_source: int | None = None,
    open_window: bool = False,
    lookback_hours: int | None = None,
) -> dict[str, Any]:
    """Собрать, отфильтровать и оценить материалы один раз для всех подписчиков.

    Прогон идёт сутками. Плановый берёт вчерашние сутки и, если предыдущие
    прогоны не отработали, добирает пропущенные — не больше
    ``max_catchup_days``. Ручной принимает даты: ``day`` — одни конкретные
    сутки, ``since_day``/``until_day`` — диапазон включительно, ``days_back`` —
    последние N завершившихся суток (кнопка «собрать за месяц» не может знать
    сегодняшнюю дату заранее, поэтому считает её здесь).

    ``open_window`` — запасной режим: одно открытое окно «за последние N
    часов», как было раньше; глубина берётся из ``COLLECTION_LOOKBACK_HOURS``,
    а ``lookback_hours`` её перекрывает. Режим не режется по суткам и потому
    упирается в лимиты выдачи источников — он для разовой проверки, не для
    расписания.
    """

    logger = get_run_logger()
    container = load_container(target=bootstrap_target)
    settings = container.settings
    limit = limit_per_source or settings.max_items_per_source
    run_id: uuid.UUID | None = None
    # Итог живёт снаружи try: прогон за неделю может оборваться на четвёртых
    # сутках, и терять отчёт по трём собранным незачем — по нему потом видно,
    # где именно оборвалось.
    total = IngestionStats()
    service = None
    try:
        reclaimed = await reclaim_stale_runs(container, settings.harvest_run_stale_minutes)
        if reclaimed:
            logger.warning(
                "Подобрано брошенных прогонов: %s — они висели в статусе running "
                "дольше %s мин и блокировали сбор",
                reclaimed,
                settings.harvest_run_stale_minutes,
            )

        chunks, plan_note = await _plan(
            container,
            settings,
            trigger,
            day,
            since_day,
            until_day,
            days_back,
            max_catchup_days,
            open_window,
            lookback_hours,
        )
        logger.info(plan_note)
        if not chunks:
            # Не ошибка и не повод открывать прогон: вчерашние сутки уже в
            # корпусе. Пустая запись в истории прогонов только мешала бы
            # читать её глазами.
            return {"skipped": "nothing-to-collect", "note": plan_note}

        run_id = await open_run(container, trigger, chunks[0].since, chunks[-1].until)
        logger.info(
            "Прогон %s открыт: %s, окно %s → %s",
            run_id,
            _days_word(len(chunks)),
            chunks[0].since.isoformat(),
            chunks[-1].until.isoformat() if chunks[-1].until else "до свежего",
        )
        service = container.ingestion_service(run_id=run_id, logger=logger)
        for index, chunk in enumerate(chunks, start=1):
            label = chunk.label
            logger.info("── %s (%s из %s) ──", label, index, len(chunks))
            stats = await service.ingest(
                since=chunk.since, until=chunk.until, limit_per_source=limit, label=label
            )
            logger.info(
                "%s: собрано %s, гейт пропустил %s, новых в корпусе %s, "
                "оценено %s, разобрано глубоко %s%s",
                label,
                stats.collected,
                stats.gate_accepted + stats.gate_borderline,
                stats.created,
                stats.ranked,
                stats.analyzed,
                f", ошибок {len(stats.failures)}" if stats.failures else "",
            )
            for name, message in stats.failures.items():
                logger.error("%s: %s — %s", label, name, message)
            broken = sorted(key for key, report in stats.sources.items() if report.get("error"))
            if broken and chunk.day_key:
                # Прогон закроется успешно и сдвинет границу собранного:
                # источник, лежащий неделю, иначе заставлял бы каждую ночь
                # переспрашивать все четыре и никогда не давал бы корпусу
                # расти. Но дыра остаётся, и добирают её руками — командой из
                # этой самой строки.
                logger.warning(
                    "%s: сутки закрыты с дырой по источникам (%s). Добрать: %s",
                    label,
                    ", ".join(broken),
                    f"geonexa collect --day {chunk.day_key}",
                )
            total.merge(stats)
            total.days.append(
                {
                    "day": label,
                    "collected": stats.collected,
                    "created": stats.created,
                    "ranked": stats.ranked,
                    "analyzed": stats.analyzed,
                    "failures": sorted(stats.failures),
                }
            )
            try:
                await heartbeat(container, run_id, index, len(chunks))
            except Exception as error:  # отметка о жизни не важнее самого сбора
                logger.warning("Отметка прогона не записана: %s: %s", type(error).__name__, error)

        # Журнал решений и счётчики терминов копятся пачками: без сброса
        # последняя пачка не доехала бы до базы.
        await service.flush_journals()
        payload = total.as_dict()
        await close_run(container, run_id, "succeeded", payload, None)
        _report(logger, total, chunks)
        return payload
    except Exception as error:
        if run_id is not None:
            if service is not None:
                # Последняя неполная пачка решений и счётчиков терминов копится
                # в памяти. Прогон оборвался — именно про него потом и хотят
                # понять, что отсеялось, а без сброса эти записи не доедут.
                await service.flush_journals()
            await close_run(container, run_id, "failed", total.as_dict(), str(error))
        logger.error("Прогон оборван: %s: %s", type(error).__name__, error)
        raise
    finally:
        await container.close()


async def _plan(
    container: Any,
    settings: Any,
    trigger: str,
    day: str | None,
    since_day: str | None,
    until_day: str | None,
    days_back: int | None,
    max_catchup_days: int | None,
    open_window: bool,
    lookback_hours: int | None,
) -> tuple[list[_Chunk], str]:
    """Какие окна собирать и почему именно их — второе для лога.

    Разбор параметров вынесен из флоу: у него четыре режима, и в теле флоу они
    превращались бы в лестницу условий посреди оркестрации.
    """

    if open_window or lookback_hours:
        hours = lookback_hours or settings.collection_lookback_hours
        since = datetime.now(UTC) - timedelta(hours=hours)
        return (
            [_Chunk(label=f"последние {hours} ч", since=since, until=None)],
            f"Режим открытого окна: последние {hours} ч, без нарезки по суткам. "
            "Верхней границы нет, лимит выдачи источника может срезать хвост.",
        )

    # Явные даты сильнее «последних N суток»: форма запуска в админке
    # подставляет параметры расписания, и человек, вписавший диапазон, не
    # должен ещё и вспоминать, что надо очистить days_back.
    first = parse_day(day or since_day)
    last = parse_day(until_day or day)
    if first is not None or last is not None:
        if first is None or last is None:
            given = first or last
            assert given is not None
            first = last = given
        windows = day_range(first, last)
        if len(windows) > MAX_MANUAL_DAYS:
            # Опечатка в дате стоит дороже отказа: 2020 вместо 2026 — это
            # больше двух тысяч суток и по четыре запроса на каждые, то есть
            # часы работы и почти наверняка бан на источниках.
            raise ValueError(
                f"Запрошено {_days_word(len(windows))} — больше предела "
                f"{MAX_MANUAL_DAYS} за один прогон. Разбейте на части: "
                f"это защита от опечатки в дате, а не ограничение платформы."
            )
        return (
            [_chunk(window) for window in windows],
            f"Заданные сутки: {windows[0].label} — {windows[-1].label}, "
            f"всего {_days_word(len(windows))}.",
        )

    if days_back:
        # «Последние N суток» — то же самое, что диапазон, только даты
        # считаются в момент запуска. Кнопка в админке статична и сегодняшнего
        # числа не знает.
        yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
        first = yesterday - timedelta(days=max(1, int(days_back)) - 1)
        windows = day_range(first, yesterday)
        if len(windows) > MAX_MANUAL_DAYS:
            raise ValueError(
                f"Запрошено {_days_word(len(windows))} — больше предела {MAX_MANUAL_DAYS} "
                "за один прогон. Разбейте на части."
            )
        return (
            [_chunk(window) for window in windows],
            f"Последние {_days_word(len(windows))}: {windows[0].label} — {windows[-1].label}.",
        )

    covered = await last_covered_until(container)
    limit_days = max_catchup_days or settings.harvest_max_catchup_days
    windows = scheduled_windows(
        now=datetime.now(UTC),
        covered_until=covered,
        max_days=limit_days,
        force=trigger != "schedule",
    )
    if not windows:
        return [], (
            f"Собирать нечего: корпус закрыт по {covered:%d.%m.%Y %H:%M} — "
            "вчерашние сутки уже собраны."
        )
    kind = "Плановый сбор" if trigger == "schedule" else "Ручной сбор"
    note = f"{kind}: {windows[0].label} — {windows[-1].label}"
    if len(windows) > 1:
        note += (
            f" ({_days_word(len(windows))}: догоняем пропущенное, предел догона {limit_days} дн.)"
        )
    if covered is not None:
        first_missed = covered.astimezone(UTC).date()
        last_missed = windows[0].day - timedelta(days=1)
        if last_missed >= first_missed:
            # Предел догона отрезал часть провала. Молчать нельзя: эти сутки
            # уже не соберёт никто — граница собранного уедет вперёд.
            skipped = (last_missed - first_missed).days + 1
            note += (
                f" ВНИМАНИЕ: {_days_word(skipped)} до окна догона пропущены и сами "
                f"не соберутся, добрать вручную: geonexa collect "
                f"--since-day {first_missed:%Y-%m-%d} --until-day {last_missed:%Y-%m-%d}"
            )
    return [_chunk(window) for window in windows], note + "."


def _report(logger: Any, total: IngestionStats, chunks: list[_Chunk]) -> None:
    """Итог прогона: по строке на источник и одна общая.

    Этот кусок и есть ответ на «где какая ошибка вылезла»: в конце хвоста
    видно каждый источник поимённо, сколько он дал и чем закончил.
    """

    logger.info("── итог за %s ──", _days_word(len(chunks)))
    for key, report in sorted(total.sources.items()):
        seconds = report.get("seconds", 0)
        collected = _plural(report.get("collected", 0), ("материал", "материала", "материалов"))
        if report.get("error"):
            logger.error(
                "%s: %s, %s с, последняя ошибка — %s", key, collected, seconds, report["error"]
            )
        else:
            windows = _plural(report.get("windows", 1), ("окно", "окна", "окон"))
            logger.info("%s: %s за %s, %s с", key, collected, windows, seconds)
    logger.info(
        "Всего: собрано %s, гейт принял %s / пограничных %s / отклонил %s, "
        "новых в корпусе %s, оценено %s, разобрано глубоко %s",
        total.collected,
        total.gate_accepted,
        total.gate_borderline,
        total.gate_rejected,
        total.created,
        total.ranked,
        total.analyzed,
    )
    # Считаются источники, а не строки в failures: ключ ошибки у источника
    # один на все сутки, и «ошибок 1» после недели с лежащим GitHub означало
    # бы «упал один раз», а не «не отвечал семь дней подряд».
    broken = sorted(key for key, report in total.sources.items() if report.get("error"))
    if broken:
        logger.warning(
            "Источники с ошибкой: %s. Дыры в корпусе за эти сутки добираются "
            "вручную: geonexa collect --day ГГГГ-ММ-ДД",
            ", ".join(broken),
        )


def _chunk(window: Any) -> _Chunk:
    """Окно суток из планировщика — в проход конвейера.

    «UTC» в подписи стоит не для педантизма: сутки сбора считаются в UTC,
    потому что датные фильтры источников тоже про UTC, и администратор в
    Москве должен видеть, что 30.08 в отчёте — это не его календарный день.
    """

    return _Chunk(
        label=f"сутки {window.label} UTC",
        since=window.since,
        until=window.until,
        day_key=window.day.isoformat(),
    )


def _days_word(count: int) -> str:
    return _plural(count, ("день", "дня", "дней"))


def _plural(count: int, forms: tuple[str, str, str]) -> str:
    """Число со словом в правильной форме.

    Мелочь, но лог читают каждый день, и «2 окон» в нём спотыкает взгляд
    ровно так же, как опечатка в интерфейсе.
    """

    tail = count % 100
    if 11 <= tail <= 14:
        return f"{count} {forms[2]}"
    last = count % 10
    if last == 1:
        return f"{count} {forms[0]}"
    if 2 <= last <= 4:
        return f"{count} {forms[1]}"
    return f"{count} {forms[2]}"
