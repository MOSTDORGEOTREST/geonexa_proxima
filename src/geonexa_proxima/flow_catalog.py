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
class FlowSpec:
    """Что именно регистрируется в Prefect."""

    key: str
    name: str
    entrypoint: str
    description: str
    schedule_kind: str
    parameters: dict[str, Any] | None = None


# Ключ совпадает с schedules.key: по нему админка связывает строку расписания
# с deployment и умеет запускать его вручную.
FLOWS: tuple[FlowSpec, ...] = (
    FlowSpec(
        key="global-harvest",
        name="geonexa-global-harvest",
        entrypoint="geonexa_proxima.workflows.harvest:global_harvest_flow",
        description="Сбор материалов из внешних источников и глобальная оценка",
        schedule_kind="global_harvest",
    ),
    FlowSpec(
        key="digest-dispatch",
        name="geonexa-digest-dispatch",
        entrypoint="geonexa_proxima.workflows.dispatch:digest_dispatch_flow",
        description="Найти личные профили, которым пора, и запустить их флоу",
        schedule_kind="digest_dispatch",
        parameters={"kinds": ["user"]},
    ),
    FlowSpec(
        key="digest-dispatch-chats",
        name="geonexa-digest-dispatch",
        entrypoint="geonexa_proxima.workflows.dispatch:digest_dispatch_flow",
        description="То же для групп и каналов: своя частота и свои лимиты",
        schedule_kind="digest_dispatch",
        parameters={"kinds": ["group", "channel"]},
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
    ),
    FlowSpec(
        key="subscription-maintenance",
        name="geonexa-subscription-maintenance",
        entrypoint="geonexa_proxima.workflows.chats:subscription_maintenance_flow",
        description="Гашение просроченных подписок и напоминания о продлении",
        schedule_kind="maintenance",
    ),
    FlowSpec(
        key="metrics-rollup",
        name="geonexa-metrics-rollup",
        entrypoint="geonexa_proxima.workflows.metrics:metrics_rollup_flow",
        description="Пересчёт суточных агрегатов",
        schedule_kind="maintenance",
    ),
    FlowSpec(
        key="maintenance",
        name="geonexa-delivery-maintenance",
        entrypoint="geonexa_proxima.workflows.delivery:delivery_maintenance_flow",
        description="Протухшие и зависшие задания рассылки",
        schedule_kind="maintenance",
    ),
)

BY_KEY: dict[str, FlowSpec] = {spec.key: spec for spec in FLOWS}
