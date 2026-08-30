"""Prefect-флоу: только оркестрация, вся логика — в сервисах."""

from geonexa_proxima.workflows.chats import chat_monitor_flow, subscription_maintenance_flow
from geonexa_proxima.workflows.delivery import (
    delivery_group_flow,
    delivery_maintenance_flow,
    delivery_personal_flow,
)
from geonexa_proxima.workflows.dispatch import digest_dispatch_flow, subscriber_digest_flow
from geonexa_proxima.workflows.harvest import global_harvest_flow
from geonexa_proxima.workflows.metrics import metrics_rollup_flow

__all__ = [
    "chat_monitor_flow",
    "delivery_group_flow",
    "delivery_maintenance_flow",
    "delivery_personal_flow",
    "digest_dispatch_flow",
    "global_harvest_flow",
    "metrics_rollup_flow",
    "subscriber_digest_flow",
    "subscription_maintenance_flow",
]
