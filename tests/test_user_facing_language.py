"""Текст, который видит человек, — на русском.

Продукт русскоязычный, но первые прототипы бота были на английском, и остатки
той разметки прожили до продакшена: подписи кнопок, заголовки дайджеста,
ответы на команды. Тест смотрит именно на строки, уходящие в Telegram, —
внутренние сообщения об ошибках и промпты к модели он не трогает.
"""

from __future__ import annotations

import ast
import pathlib
import re

from geonexa_proxima.domain import FeedbackKind
from geonexa_proxima.telegram.bot import _FEEDBACK_LABELS
from geonexa_proxima.telegram.keyboards import FEEDBACK_CODES, feedback_keyboard

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "geonexa_proxima"
CYRILLIC = re.compile("[А-Яа-яЁё]")
LATIN_WORD = re.compile("[A-Za-z]{3,}")

#: Куда уходит текст для человека.
ANSWERING = {"answer", "reply", "edit_text"}


def _outgoing_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []

    def literal_parts(node: ast.AST) -> list[str]:
        """Строковые куски выражения: константы и f-строки, склеенные конкатенацией."""

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return [v.value for v in node.values if isinstance(v, ast.Constant)]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal_parts(node.left) + literal_parts(node.right)
        return []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ANSWERING:
            for argument in node.args:
                found.extend((node.lineno, part) for part in literal_parts(argument))
        elif name == "BotCommand":
            for keyword in node.keywords:
                if keyword.arg == "description":
                    found.extend((node.lineno, part) for part in literal_parts(keyword.value))
    return found


def test_bot_speaks_russian() -> None:
    suspicious: list[str] = []
    for path in (SRC / "telegram" / "bot.py", SRC / "telegram" / "chats.py"):
        if not path.exists():
            continue
        for lineno, text in _outgoing_strings(path):
            stripped = text.strip()
            # Куски вёрстки и подстановки в f-строках сами по себе текста не несут.
            if len(stripped) < 8 or not LATIN_WORD.search(stripped):
                continue
            if CYRILLIC.search(stripped):
                continue
            suspicious.append(f"{path.name}:{lineno}: {stripped[:70]}")
    assert not suspicious, "Ответы бота не на русском:\n" + "\n".join(suspicious)


def test_digest_headings_are_russian() -> None:
    source = (SRC / "services" / "digest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    labels: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            labels.extend(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
    headings = [value for value in labels if len(value) > 3]
    assert headings, "в форматтере дайджеста не нашлось подписей разделов"
    assert all(CYRILLIC.search(value) for value in headings), headings


def test_every_feedback_kind_has_a_button_and_a_label() -> None:
    """Новая реакция не должна появиться без кнопки и без подписи.

    Иначе она либо недостижима из Telegram, либо в подтверждении вылезает
    машинное имя вроде `very_interesting`.
    """

    from_buttons = {FeedbackKind(value) for value in FEEDBACK_CODES.values()}
    assert from_buttons == set(FeedbackKind), set(FeedbackKind) - from_buttons
    assert set(_FEEDBACK_LABELS) == set(FeedbackKind)
    assert all(CYRILLIC.search(label) for label in _FEEDBACK_LABELS.values())


def test_bot_and_delivery_share_one_keyboard() -> None:
    """Бот и воркер рассылки рисуют одну и ту же разметку.

    Расхождение означало бы, что кнопка из планового дайджеста не находит
    обработчика — то есть обратная связь молча теряется у всех, кто не
    запрашивал материал командой.
    """

    from uuid import uuid4

    from geonexa_proxima.telegram.bot import _item_keyboard

    score_id = uuid4()
    plain = feedback_keyboard(score_id)
    typed = _item_keyboard(score_id)

    assert [[button["callback_data"] for button in row] for row in plain["inline_keyboard"]] == [
        [button.callback_data for button in row] for row in typed.inline_keyboard
    ]
    assert [[button["text"] for button in row] for row in plain["inline_keyboard"]] == [
        [button.text for button in row] for row in typed.inline_keyboard
    ]
