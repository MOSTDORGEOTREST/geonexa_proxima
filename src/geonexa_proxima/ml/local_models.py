"""Поиск локальных весов в каталоге ``models/``.

Каталог называют как удобно: ``Qwen3-Embedding-0.6B``, ``qwen3-emb-0.6b``,
``qwen3_embedding_06b``. Требовать точного имени в ``.env`` — значит ловить
«модель не найдена» после каждой перекачки. Поэтому веса ищутся по содержимому:
берётся каталог с ``config.json``, а размерность читается из него же, а не
угадывается по имени.

Три вещи проверяются до первого запроса, потому что на середине сбора об этом
узнавать поздно: каталог существует, веса на месте, размерность совпадает с
той, под которую заведены колонки pgvector.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def preferred_dtype(device: str | None = None) -> str:
    """Тип весов под то железо, на котором реально считаем.

    `float16` быстр на GPU и почти бесполезен на CPU: половина операций там не
    имеет нативной реализации, часть версий torch прямо падает с «not
    implemented for Half», остальные считают через эмуляцию. На CPU берём
    `bfloat16`: тот же выигрыш вдвое по памяти (0.6B модель занимает 1.2 ГБ
    вместо 2.4), но без ловушек. Именно из-за этого стенд на ноутбуке съедал
    память и не доходил до конца сбора.
    """

    target = (device or "").lower()
    if target:
        return "float16" if target.startswith(("cuda", "mps")) else "bfloat16"
    try:
        import torch

        if torch.cuda.is_available():
            return "float16"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "float16"
    except Exception:
        # torch не импортировался — решать всё равно нечего, вернём безопасное.
        return "float32"
    return "bfloat16"


WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt")
EMBEDDING_HINTS = ("embedding", "emb")
RERANKER_HINTS = ("reranker", "rerank")


@dataclass(frozen=True, slots=True)
class LocalModel:
    path: Path
    hidden_size: int | None
    model_type: str | None
    has_weights: bool
    is_sentence_transformer: bool

    @property
    def name(self) -> str:
        return self.path.name

    def describe(self) -> str:
        parts = [str(self.path)]
        if self.hidden_size:
            parts.append(f"{self.hidden_size} измерений")
        if not self.has_weights:
            parts.append("БЕЗ ВЕСОВ")
        return " · ".join(parts)


def read_local_model(path: Path) -> LocalModel | None:
    """Прочитать каталог как модель. None, если это не она."""

    config = path / "config.json"
    if not config.is_file():
        return None
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    hidden = payload.get("hidden_size")
    return LocalModel(
        path=path,
        hidden_size=int(hidden) if isinstance(hidden, int) else None,
        model_type=payload.get("model_type"),
        has_weights=any(
            child.suffix in WEIGHT_SUFFIXES for child in path.iterdir() if child.is_file()
        ),
        is_sentence_transformer=(path / "modules.json").is_file(),
    )


def discover_models(root: Path) -> list[LocalModel]:
    """Все модели в каталоге, включая вложенные на один уровень (org/model)."""

    if not root.is_dir():
        return []
    found: list[LocalModel] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        model = read_local_model(child)
        if model is not None:
            found.append(model)
            continue
        for grandchild in sorted(child.iterdir()):
            if grandchild.is_dir():
                nested = read_local_model(grandchild)
                if nested is not None:
                    found.append(nested)
    return found


def _matches(name: str, hints: tuple[str, ...], exclude: tuple[str, ...]) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in exclude):
        return False
    return any(marker in lowered for marker in hints)


def resolve_local_model(
    configured: Path,
    root: Path,
    *,
    kind: str,
) -> LocalModel | None:
    """Найти веса: сначала по заданному пути, затем по содержимому каталога.

    ``kind`` — ``embedding`` или ``reranker``. Реранкер отсекается от эмбеддера
    по имени: оба каталога лежат рядом и оба содержат ``config.json``.
    """

    direct = read_local_model(configured)
    if direct is not None:
        return direct

    candidates = discover_models(root)
    if not candidates:
        return None
    if kind == "embedding":
        hints, exclude = EMBEDDING_HINTS, RERANKER_HINTS
    else:
        hints, exclude = RERANKER_HINTS, ()

    matched = [model for model in candidates if _matches(model.name, hints, exclude)]
    if kind == "embedding":
        # У эмбеддера есть modules.json — это надёжнее имени каталога.
        sentence_transformers = [model for model in matched if model.is_sentence_transformer]
        if sentence_transformers:
            matched = sentence_transformers
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        # Несколько подходящих — берём с весами и самый свежий.
        with_weights = [model for model in matched if model.has_weights] or matched
        return max(with_weights, key=lambda model: model.path.stat().st_mtime)
    return None


class LocalModelError(RuntimeError):
    """Веса не найдены или не подходят под текущую конфигурацию."""


def require_local_model(
    configured: Path,
    root: Path,
    *,
    kind: str,
    expected_dimensions: int | None = None,
) -> LocalModel:
    """Найти веса и проверить их пригодность, иначе внятно отказаться."""

    model = resolve_local_model(configured, root, kind=kind)
    if model is None:
        available = discover_models(root)
        listing = ", ".join(item.name for item in available) if available else "каталог пуст"
        raise LocalModelError(
            f"Веса {kind} не найдены: ни по пути {configured}, ни в {root} ({listing}). "
            f"Скачай модель в {root} или переключись на EMBEDDING_MODE=api."
        )
    if not model.has_weights:
        raise LocalModelError(
            f"В {model.path} нет файлов весов ({', '.join(WEIGHT_SUFFIXES)}): "
            f"похоже, загрузка оборвалась."
        )
    if (
        kind == "embedding"
        and expected_dimensions is not None
        and model.hidden_size is not None
        and expected_dimensions > model.hidden_size
    ):
        raise LocalModelError(
            f"EMBEDDING_DIMENSIONS={expected_dimensions} больше нативной размерности "
            f"модели в {model.path} ({model.hidden_size}). Matryoshka режет вниз, "
            f"но не удлиняет."
        )
    return model
