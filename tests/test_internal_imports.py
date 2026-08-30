"""Внутренние импорты пакета: имя, которого нет, ловим до запуска.

Такой дефект незаметен для линтера и для тестов, которые модуль не трогают:
`from geonexa_proxima.flow_catalog import WORK_POOL_DEFAULT` падает только в
момент, когда воркер впервые пытается зарегистрировать деплойменты, и падение
съедает `|| echo` в entrypoint — сервис стартует, но половина его работы молча
не выполняется.

Разбор статический (`ast`): ничего не импортируется по-настоящему, поэтому
проверка не зависит ни от конфигурации, ни от того, установлен ли Prefect.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
PACKAGE = "geonexa_proxima"


def _modules() -> dict[str, pathlib.Path]:
    found: dict[str, pathlib.Path] = {}
    for path in SRC.rglob("*.py"):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def _collect(body: list[ast.stmt], names: set[str], flags: set[str]) -> None:
    """Имена, связанные на уровне модуля.

    В тела `if` и `try` заходим: `if TYPE_CHECKING:` и `try: import ...` —
    обычный способ объявить имя, и пропускать такие модули целиком значило бы
    не проверять их вовсе.
    """

    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    flags.add("star")
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If):
            _collect(node.body, names, flags)
            _collect(node.orelse, names, flags)
        elif isinstance(node, ast.Try):
            for block in (node.body, node.orelse, node.finalbody):
                _collect(block, names, flags)
            for handler in node.handlers:
                _collect(handler.body, names, flags)


def _exports(path: pathlib.Path) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    flags: set[str] = set()
    _collect(ast.parse(path.read_text(encoding="utf-8")).body, names, flags)
    if "__getattr__" in names:
        # Ленивый реэкспорт (api/__init__.py): состав имён известен только в
        # рантайме, статически проверить нечего.
        flags.add("getattr")
    return names, flags


def test_every_internal_import_resolves() -> None:
    modules = _modules()
    exports: dict[str, tuple[set[str], set[str]]] = {}
    broken: list[str] = []

    for path in sorted(modules.values()):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            target = node.module or ""
            if not target.startswith(PACKAGE):
                continue
            where = f"{path.relative_to(SRC)}:{node.lineno}"
            if target not in modules:
                broken.append(f"{where}: модуля {target} не существует")
                continue
            if target not in exports:
                exports[target] = _exports(modules[target])
            names, flags = exports[target]
            if flags:
                continue
            for alias in node.names:
                if alias.name in names or f"{target}.{alias.name}" in modules:
                    continue
                broken.append(f"{where}: {target} не определяет {alias.name}")

    assert not broken, "Нерешаемые импорты внутри пакета:\n" + "\n".join(broken)
