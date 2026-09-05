"""Каталог флоу: данные, а не оркестрация.

Лежит в корне пакета, а не в ``workflows``: импорт любого модуля оттуда
поднимает ``workflows/__init__``, который тянет Prefect. Админка общается с
Prefect по REST и не должна требовать его как зависимость только ради списка
имён — иначе admin API не поднимется там, где библиотеки нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Имя рабочего пула по умолчанию. Совпадает с ``Settings.prefect_work_pool``,
# но живёт здесь: регистрации деплойментов нужен запасной вариант на случай,
# когда PREFECT_WORK_POOL пуст, а тянуть ради одной строки настройки нельзя —
# каталог обязан импортироваться без конфигурации.
WORK_POOL_DEFAULT = "geonexa-pool"
WORK_QUEUE_DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Один параметр запуска — так, как его показывает админка.

    Флоу принимают параметры именованными аргументами, и до этого описания
    единственным способом их задать был JSON руками — с именами, которые надо
    было подсмотреть в коде. Здесь имя, тип, подпись и подсказка в одном
    месте: форма в админке и валидация на сервере строятся из него.
    """

    key: str
    label: str
    #: int | float | bool | str | date | list — как парсить строку из формы.
    type: str = "str"
    hint: str = ""
    default: Any = None
    #: Допустимые значения для выпадающего списка; пусто — свободный ввод.
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class FlowSpec:
    """Что именно регистрируется в Prefect."""

    key: str
    name: str
    entrypoint: str
    description: str
    schedule_kind: str
    parameters: dict[str, Any] | None = None
    #: Какие параметры принимает флоу. Пустой кортеж — параметров нет.
    fields: tuple[ParamSpec, ...] = ()


_DISPATCH_FIELDS: tuple[ParamSpec, ...] = (
    ParamSpec(
        "kinds",
        "Виды подписчиков",
        "list",
        "Через запятую: user, group, channel. Пусто — все.",
    ),
    ParamSpec(
        "deliver_at_hour",
        "Час отправки",
        "int",
        "Час по поясу платформы, на который назначается доставка. Пусто — сразу.",
        minimum=0,
        maximum=23,
    ),
    ParamSpec("limit", "Не больше профилей", "int", minimum=1, maximum=10000, default=500),
    ParamSpec("concurrency", "Параллельно", "int", minimum=1, maximum=64, default=8),
    ParamSpec(
        "deliver",
        "Ставить в очередь доставки",
        "bool",
        "Выключено — дайджест собирается, но никуда не уходит (проба).",
        default=True,
    ),
)

# Ключ совпадает с schedules.key: по нему админка связывает строку расписания
# с deployment и умеет запускать его вручную.
FLOWS: tuple[FlowSpec, ...] = (
    FlowSpec(
        key="global-harvest",
        name="geonexa-global-harvest",
        entrypoint="geonexa_proxima.workflows.harvest:global_harvest_flow",
        description="Сбор материалов из внешних источников и глобальная оценка",
        schedule_kind="global_harvest",
        fields=(
            ParamSpec(
                "days_back",
                "Последние N суток",
                "int",
                "Сколько завершившихся суток собрать, считая от вчерашних. "
                "Пусто — плановый режим: вчерашние сутки плюс догон пропущенных.",
                minimum=1,
                maximum=400,
            ),
            ParamSpec(
                "since_day",
                "С даты",
                "date",
                "Начало диапазона (UTC, включительно). Сильнее, чем «последние N суток».",
            ),
            ParamSpec("until_day", "По дату", "date", "Конец диапазона (UTC, включительно)."),
            ParamSpec(
                "max_catchup_days",
                "Предел догона",
                "int",
                "Сколько пропущенных суток добирает один плановый прогон.",
                minimum=1,
                maximum=400,
            ),
            ParamSpec(
                "limit_per_source",
                "Лимит на источник",
                "int",
                "Сколько материалов брать у каждого источника за сутки.",
                minimum=1,
                maximum=5000,
            ),
        ),
    ),
    FlowSpec(
        key="digest-dispatch",
        name="geonexa-digest-dispatch",
        entrypoint="geonexa_proxima.workflows.dispatch:digest_dispatch_flow",
        description="Найти личные профили, которым пора, и запустить их флоу",
        schedule_kind="digest_dispatch",
        parameters={"kinds": ["user"]},
        fields=_DISPATCH_FIELDS,
    ),
    FlowSpec(
        key="digest-dispatch-chats",
        name="geonexa-digest-dispatch",
        entrypoint="geonexa_proxima.workflows.dispatch:digest_dispatch_flow",
        description="То же для групп и каналов: своя частота и свои лимиты",
        schedule_kind="digest_dispatch",
        # Собирается ночью, уходит в 16:00 по поясу платформы. Час живёт в
        # параметрах задания, а не в кроне воркера рассылки: воркер должен
        # крутиться часто, иначе повторная попытка после сбоя ждёт неделю.
        parameters={"kinds": ["group", "channel"], "deliver_at_hour": 16},
        fields=_DISPATCH_FIELDS,
    ),
    FlowSpec(
        key="subscriber-digest",
        name="geonexa-subscriber-digest",
        entrypoint="geonexa_proxima.workflows.dispatch:subscriber_digest_flow",
        description="Дайджест одного профиля; запускается диспетчером",
        schedule_kind="subscriber_digest",
    ),
    FlowSpec(
        key="delivery-personal",
        name="geonexa-delivery-personal",
        entrypoint="geonexa_proxima.workflows.delivery:delivery_personal_flow",
        description="Рассылка дайджестов в личные чаты",
        schedule_kind="delivery_personal",
    ),
    FlowSpec(
        key="delivery-group",
        name="geonexa-delivery-group",
        entrypoint="geonexa_proxima.workflows.delivery:delivery_group_flow",
        description="Рассылка дайджестов в группы и каналы",
        schedule_kind="delivery_group",
    ),
    FlowSpec(
        key="chat-monitor",
        name="geonexa-chat-monitor",
        entrypoint="geonexa_proxima.workflows.chats:chat_monitor_flow",
        description="Сверка прав бота во всех группах и каналах",
        schedule_kind="chat_monitor",
        fields=(
            ParamSpec("limit", "Чатов за страницу", "int", minimum=1, maximum=1000, default=200),
            ParamSpec(
                "pause_seconds",
                "Пауза между чатами, с",
                "float",
                "Чтобы не упереться в лимиты Bot API.",
                minimum=0,
                maximum=10,
                default=0.2,
            ),
        ),
    ),
    FlowSpec(
        key="subscription-maintenance",
        name="geonexa-subscription-maintenance",
        entrypoint="geonexa_proxima.workflows.chats:subscription_maintenance_flow",
        description="Гашение просроченных подписок и напоминания о продлении",
        schedule_kind="maintenance",
        fields=(
            ParamSpec(
                "remind_within_days",
                "Напоминать за N дней",
                "int",
                "За сколько дней до окончания подписки ставить напоминание.",
                minimum=0,
                maximum=60,
                default=3,
            ),
        ),
    ),
    FlowSpec(
        key="metrics-rollup",
        name="geonexa-metrics-rollup",
        entrypoint="geonexa_proxima.workflows.metrics:metrics_rollup_flow",
        description="Пересчёт суточных агрегатов",
        schedule_kind="maintenance",
        fields=(
            ParamSpec("day_from", "С даты", "date", "Пусто — вчерашние сутки."),
            ParamSpec("day_to", "По дату", "date"),
            ParamSpec(
                "scope",
                "Разрез",
                "str",
                "all — все агрегаты; иначе один: harvest, subscribers, delivery, "
                "engagement, retention.",
                default="all",
                choices=("all", "harvest", "subscribers", "delivery", "engagement", "retention"),
            ),
        ),
    ),
    FlowSpec(
        key="maintenance",
        name="geonexa-delivery-maintenance",
        entrypoint="geonexa_proxima.workflows.delivery:delivery_maintenance_flow",
        description="Протухшие и зависшие задания рассылки",
        schedule_kind="maintenance",
        fields=(
            ParamSpec(
                "purge_old_rows",
                "Чистить сырые события",
                "bool",
                "Удалять решения гейта и события старше срока хранения.",
                default=True,
            ),
        ),
    ),
)

BY_KEY: dict[str, FlowSpec] = {spec.key: spec for spec in FLOWS}


def field_specs(key: str) -> tuple[ParamSpec, ...]:
    spec = BY_KEY.get(key)
    return spec.fields if spec else ()


def describe_fields(spec: FlowSpec) -> list[dict[str, Any]]:
    """Поля параметров в виде, который отдаётся админке."""

    return [
        {
            "key": field.key,
            "label": field.label,
            "type": field.type,
            "hint": field.hint,
            "default": field.default,
            "choices": list(field.choices),
            "minimum": field.minimum,
            "maximum": field.maximum,
        }
        for field in spec.fields
    ]


def coerce_parameters(key: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Привести параметры из формы к типам флоу.

    Форма присылает строки; флоу ждут числа, даты и списки. Пустая строка
    означает «не задано» и из параметров выбрасывается — иначе `days_back=""`
    уехал бы в Prefect и уронил прогон на валидации уже после постановки.
    Ключи, которых нет в описании, пропускаются как есть: у JSON-редактора
    должна оставаться возможность передать то, что форма не знает.
    """

    known = {field.key: field for field in field_specs(key)}
    result: dict[str, Any] = {}
    for name, value in raw.items():
        field = known.get(name)
        if field is None:
            if value not in ("", None):
                result[name] = value
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        result[name] = _coerce(field, value)
    return result


def _coerce(field: ParamSpec, value: Any) -> Any:
    kind = field.type
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "on", "yes", "да"}
    if kind == "int":
        number = int(float(str(value).strip()))
        _check_range(field, number)
        return number
    if kind == "float":
        number = float(str(value).strip())
        _check_range(field, number)
        return number
    if kind == "date":
        from datetime import date

        text = str(value).strip()
        date.fromisoformat(text)  # ValueError, если дата не разбирается
        return text
    if kind == "list":
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
        else:
            items = [item.strip() for item in str(value).split(",")]
        return [item for item in items if item]
    text = str(value).strip()
    if field.choices and text not in field.choices:
        raise ValueError(f"{field.label}: одно из {', '.join(field.choices)}")
    return text


def _check_range(field: ParamSpec, number: float) -> None:
    if field.minimum is not None and number < field.minimum:
        raise ValueError(f"{field.label}: не меньше {field.minimum:g}")
    if field.maximum is not None and number > field.maximum:
        raise ValueError(f"{field.label}: не больше {field.maximum:g}")
