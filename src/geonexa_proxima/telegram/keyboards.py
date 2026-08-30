"""Кнопки обратной связи под материалом.

Живут отдельно от бота: плановый дайджест отправляет воркер доставки, а не
бот, и без общего места клавиатуры доезжали бы только до тех сообщений,
которые человек запросил командой. Обратная связь — вход обучаемого профиля,
и дайджест без кнопок делает весь feedback loop недостижимым.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

#: Короткие коды в callback_data. Telegram ограничивает её 64 байтами, а UUID
#: занимает 36 — на слова места нет.
FEEDBACK_CODES = {
    "vi": "very_interesting",
    "u": "useful",
    "ni": "not_interesting",
    "s": "save",
    "d": "deeper",
}


def feedback_keyboard(profile_score_id: UUID | str) -> dict[str, Any]:
    """Клавиатура в виде обычного словаря.

    Словарь, а не объект aiogram: этот модуль импортируется воркером доставки,
    который не должен тянуть за собой бота ради разметки. Bot API принимает
    такую структуру как есть.
    """

    suffix = str(profile_score_id)
    return {
        "inline_keyboard": [
            [
                {"text": "Очень интересно", "callback_data": f"fb:vi:{suffix}"},
                {"text": "Полезно", "callback_data": f"fb:u:{suffix}"},
            ],
            [
                {"text": "Не моё", "callback_data": f"fb:ni:{suffix}"},
                {"text": "Сохранить", "callback_data": f"fb:s:{suffix}"},
                {"text": "Разобрать", "callback_data": f"fb:d:{suffix}"},
            ],
            [{"text": "Почему это?", "callback_data": f"pw:{suffix}"}],
        ]
    }
