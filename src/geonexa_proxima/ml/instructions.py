"""Асимметрия запросов и документов в Qwen3-Embedding.

Модель обучена так, что запрос подаётся с инструкцией, а документ — без неё.
Это не украшение промпта: инструкция задаёт задачу, под которую выравнивается
пространство. Подать документ с инструкцией или запрос без неё — значит
потерять несколько процентов качества там, где никто не будет искать причину.

Формат взят из карточки модели дословно, включая отсутствие пробела после
``Query:``: ``f"Instruct: {task}\\nQuery:{text}"``. Тот же префикс лежит в
``prompts.query`` конфигурации sentence-transformers, поэтому строка обязана
совпадать байт в байт, иначе локальный и API-режим разойдутся.
"""

from __future__ import annotations

QUERY_TEMPLATE = "Instruct: {instruction}\nQuery:{text}"


def format_query(text: str, instruction: str | None) -> str:
    """Обернуть запрос инструкцией. Без инструкции текст остаётся как есть."""

    if not instruction or not instruction.strip():
        return text
    return QUERY_TEMPLATE.format(instruction=instruction.strip(), text=text)


def format_document(text: str) -> str:
    """Документ подаётся без инструкции — сознательно и всегда."""

    return text


def query_prompt_prefix(instruction: str | None) -> str | None:
    """Префикс для sentence-transformers ``prompt=``: всё до самого текста."""

    if not instruction or not instruction.strip():
        return None
    return QUERY_TEMPLATE.format(instruction=instruction.strip(), text="")
