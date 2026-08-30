"""Регистрация деплойментов в Prefect: два места, где она молча ломалась.

Оба дефекта не видны ни линтеру, ни обычным тестам: воркер стартует, в логе
одна строка, а расписания просто не выполняются, и админка показывает пустой
список флоу.

1. `to_deployment()` в асинхронном контексте отдаёт корутину, а не
   `RunnerDeployment`. Цепочка `.to_deployment(...).apply()` падала в
   «'coroutine' object has no attribute 'apply'» — все десять флоу разом.
2. `apply()` без пула регистрирует deployment вне рабочего пула, и воркер,
   слушающий `geonexa-pool`, никогда его не подберёт.
3. Процессный воркер по умолчанию запускает флоу во временном каталоге и
   читает код из файла по пути. Кода там нет — он в установленном пакете, —
   и прогон падал на `FileNotFoundError` ещё до первой строки флоу.
"""

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

from geonexa_proxima.config import Settings
from geonexa_proxima.flow_catalog import FLOWS
from geonexa_proxima.workflows import deployments as module


class _FakeDeployment:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.applied = False

    async def apply(self) -> str:
        self.applied = True
        return str(uuid4())


class _FakeFlow:
    """Флоу, повторяющее ключевое свойство настоящего: `to_deployment` — корутина."""

    def __init__(self) -> None:
        self.deployments: list[_FakeDeployment] = []

    async def to_deployment(self, **kwargs: Any) -> _FakeDeployment:
        deployment = _FakeDeployment(kwargs)
        self.deployments.append(deployment)
        return deployment


class _FakeResult:
    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return []


class _FakeConnection:
    async def execute(self, *_: Any, **__: Any) -> _FakeResult:
        return _FakeResult()


class _FakeEngine:
    """Расписаний в базе нет — берутся значения по умолчанию из каталога."""

    @asynccontextmanager
    async def connect(self):
        yield _FakeConnection()

    @asynccontextmanager
    async def begin(self):
        yield _FakeConnection()


@pytest.mark.asyncio
async def test_deploy_all_awaits_the_deployment_before_applying(monkeypatch) -> None:
    flows: list[_FakeFlow] = []

    def fake_import(_entrypoint: str) -> _FakeFlow:
        created = _FakeFlow()
        flows.append(created)
        return created

    pools: list[str] = []

    async def fake_pool(name: str, **_: Any) -> None:
        pools.append(name)

    monkeypatch.setattr(module, "_import_flow", fake_import)
    monkeypatch.setattr(module, "ensure_work_pool", fake_pool)

    settings = Settings(prefect_work_pool="geonexa-pool", prefect_work_queue="default")
    report = await module.deploy_all(_FakeEngine(), settings)

    assert report["failed"] == {}
    assert len(report["deployed"]) == len(FLOWS)
    # Пул создаётся до регистрации: в entrypoint deploy идёт раньше воркера,
    # который этот пул обычно и создаёт.
    assert pools == ["geonexa-pool"]

    for flow in flows:
        assert len(flow.deployments) == 1
        deployment = flow.deployments[0]
        assert deployment.applied, "apply() не вызван — deployment не зарегистрирован"
        assert deployment.kwargs["work_pool_name"] == "geonexa-pool"
        assert deployment.kwargs["work_queue_name"] == "default"
        # Импорт модуля, а не чтение файла: файла рядом с процессом воркера нет.
        assert deployment.kwargs["entrypoint_type"].value == "module_path"
        # Рабочий каталог прибит к корню проекта, иначе относительные пути
        # из конфигурации (`config/harvest.yaml`, `models/…`) не разрешатся.
        working_dir = deployment.kwargs["job_variables"]["working_dir"]
        assert (pathlib.Path(working_dir) / "src" / "geonexa_proxima").is_dir(), working_dir


@pytest.mark.asyncio
async def test_failure_of_one_flow_does_not_stop_the_rest(monkeypatch) -> None:
    """Одно сломанное флоу не должно уносить остальные девять."""

    class _Broken(_FakeFlow):
        async def to_deployment(self, **kwargs: Any) -> _FakeDeployment:
            raise RuntimeError("модуль не импортируется")

    calls = {"n": 0}

    def fake_import(_entrypoint: str) -> _FakeFlow:
        calls["n"] += 1
        return _Broken() if calls["n"] == 1 else _FakeFlow()

    async def fake_pool(_name: str, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(module, "_import_flow", fake_import)
    monkeypatch.setattr(module, "ensure_work_pool", fake_pool)

    report = await module.deploy_all(_FakeEngine(), Settings())

    assert len(report["failed"]) == 1
    assert len(report["deployed"]) == len(FLOWS) - 1


def test_every_prefect_task_disables_caching() -> None:
    """Задачи принимают живой контейнер — хешировать его Prefect не умеет.

    По умолчанию Prefect считает ключ кэша по аргументам задачи. Наш контейнер
    держит движок SQLAlchemy и блокировки: ни в JSON, ни в pickle он не
    сериализуется, и каждый прогон получал в лог трейсбек `HashError` —
    страшный на вид и совершенно безобидный. Кэшировать здесь нечего: задачи
    существуют ради записи в базу.
    """

    import ast

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "geonexa_proxima" / "workflows"
    missing: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                name = call.func if call else decorator
                label = getattr(name, "id", None) or getattr(name, "attr", None)
                if label != "task":
                    continue
                keywords = {kw.arg for kw in call.keywords} if call else set()
                if "cache_policy" not in keywords:
                    missing.append(f"{path.name}:{node.lineno}: {node.name}")

    assert not missing, "У задач не задан cache_policy=NO_CACHE:\n" + "\n".join(missing)
