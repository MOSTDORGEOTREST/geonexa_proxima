"""Описание параметров флоу обязано совпадать с самими флоу.

`flow_catalog.ParamSpec` — то, из чего админка строит форму и чем API
приводит типы. Поле, которого флоу не принимает, уехало бы в Prefect и
уронило бы прогон на валидации уже после постановки в очередь — тихо для
человека, нажавшего кнопку.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

from geonexa_proxima.flow_catalog import FLOWS, coerce_parameters, describe_fields


def _signature(entrypoint: str) -> inspect.Signature:
    module_name, _, attribute = entrypoint.partition(":")
    flow = getattr(importlib.import_module(module_name), attribute)
    return inspect.signature(getattr(flow, "fn", flow))


@pytest.mark.parametrize("spec", FLOWS, ids=[spec.key for spec in FLOWS])
def test_every_field_is_a_flow_parameter(spec) -> None:
    accepted = set(_signature(spec.entrypoint).parameters)
    unknown = sorted({field.key for field in spec.fields} - accepted)
    assert not unknown, f"{spec.key}: поля не принимаются флоу — {', '.join(unknown)}"
    stored = sorted(set(spec.parameters or {}) - accepted)
    assert not stored, f"{spec.key}: параметры по умолчанию не принимаются — {', '.join(stored)}"


def test_fields_are_described_for_the_ui() -> None:
    for spec in FLOWS:
        for field in describe_fields(spec):
            assert field["key"] and field["label"]
            assert field["type"] in {"int", "float", "bool", "str", "date", "list"}


def test_coerce_types_and_drops_empty_values() -> None:
    values = coerce_parameters(
        "global-harvest",
        {"days_back": "90", "since_day": "", "limit_per_source": "1000", "extra": "x"},
    )
    assert values == {"days_back": 90, "limit_per_source": 1000, "extra": "x"}
    assert coerce_parameters("digest-dispatch", {"kinds": "user, group", "deliver": "false"}) == {
        "kinds": ["user", "group"],
        "deliver": False,
    }


def test_coerce_rejects_out_of_range_and_bad_dates() -> None:
    with pytest.raises(ValueError, match="не больше"):
        coerce_parameters("global-harvest", {"days_back": "5000"})
    with pytest.raises(ValueError):
        coerce_parameters("global-harvest", {"since_day": "вчера"})
    with pytest.raises(ValueError, match="одно из"):
        coerce_parameters("metrics-rollup", {"scope": "everything"})
