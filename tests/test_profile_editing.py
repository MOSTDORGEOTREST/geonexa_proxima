"""Правка профиля из админки: что читается, то и записывается.

Здесь закрыты две ошибки, каждая из которых портила данные молча. Редактор
заполнял поле из ответа API и сохранял его обратно — а колонки в ответе не
было, и первое же сохранение стирало описание. И нормализация схлопывала
переводы строк, из-за чего профиль, набранный по теме на строку, при следующей
перекомпиляции склеивался в одну тему.
"""

from __future__ import annotations

from geonexa_proxima.api.admin.routers import subscribers as router
from geonexa_proxima.db.user_repository import clean_description
from geonexa_proxima.services.facets import build_facets, split_sentences


def _selected_columns() -> set[str]:
    """Колонки, которыми админка наполняет редактор профиля.

    Читаем не текст функции, а её синтаксическое дерево: SQL собран из
    нескольких строковых литералов подряд, и склеивать их регуляркой значило бы
    ломаться от любого переноса, поставленного форматтером.
    """

    import ast
    import inspect

    tree = ast.parse(inspect.getsource(router.get_subscriber).strip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        query = node.value
        if "FROM subscriber_profiles" not in query or "SELECT" not in query:
            continue
        columns = query[query.index("SELECT") + 6 : query.index("FROM subscriber_profiles")]
        return {part.strip().split()[-1] for part in columns.split(",") if part.strip()}
    raise AssertionError("не нашёлся запрос профилей в карточке подписчика")


def test_editor_can_read_back_every_field_it_writes() -> None:
    """Иначе редактор отправляет пустое поле как осознанную очистку.

    Ровно так и терялось описание: колонки не было в выборке, поле в форме
    приходило пустым, и сохранение любой соседней настройки затирало текст,
    по которому работает весь отбор.
    """

    writable = set(router.ProfilePatch.model_fields)
    readable = _selected_columns()
    # paused_until и timezone редактор пока не показывает — их не проверяем.
    required = writable - {"paused_until", "timezone", "min_global_score"}

    missing = sorted(required - readable)
    assert not missing, "Редактор пишет поля, которых нет в карточке: " + ", ".join(missing)


def test_description_keeps_its_line_breaks() -> None:
    """Строка — граница темы, и нормализация не должна её съедать.

    Общая нормализация схлопывает любые пробельные символы в один пробел.
    Для описания это тихая потеря смысла: профиль, набранный по теме на строку,
    при следующей перекомпиляции превращался в одну тему, а внешне текст
    оставался прежним.
    """

    stored = clean_description(
        "  Моделирование в геотехнике: МКЭ и определяющие соотношения.  \n"
        "Разжижение   грунтов при циклических нагрузках.\n"
        "ИИ для обработки данных полевых опытов.  "
    )

    assert stored is not None
    assert stored.count("\n") == 2
    # Пробелы внутри строки схлопываются по-прежнему.
    assert "Разжижение грунтов" in stored
    assert not stored.startswith(" ") and not stored.endswith(" ")
    assert len(split_sentences(stored, min_chars=16)) == 3


def test_description_survives_a_round_trip_into_facets() -> None:
    """Сохранили — перекомпилировали — тем осталось столько же."""

    written = "Первая тема про геотехнику и модели\nВторая тема про обработку опытов ИИ"
    stored = clean_description(written)
    assert stored is not None

    before = build_facets(f"Profile description:\n{written}")
    after = build_facets(f"Profile description:\n{stored}")

    assert [facet.text for facet in before] == [facet.text for facet in after]
    assert len(after) == 2


def test_blank_description_is_still_stored_as_nothing() -> None:
    assert clean_description("   \n\n  ") is None
    assert clean_description(None) is None


def test_runs_of_blank_lines_collapse() -> None:
    """Иначе абзацный отступ в три строки раздувал бы текст без пользы."""

    stored = clean_description("Первая тема про геотехнику.\n\n\n\nВторая тема про ИИ.")

    assert stored == "Первая тема про геотехнику.\n\nВторая тема про ИИ."
