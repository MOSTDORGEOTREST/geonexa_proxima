"""Сборщик по протоколу OAI-PMH: КиберЛенинка и любой журнал на OJS.

OAI-PMH — стандарт обмена метаданными у открытых архивов: КиберЛенинка,
Elpub, любой журнал на Open Journal Systems отдают его из коробки по адресу
вида ``https://журнал/oai``. Поиска по словам в протоколе нет — есть
«всё, что появилось с даты X по дату Y», постранично. Именно это и нужно
для посуточного сбора: окно уходит источнику, а отсев делает наш гейт.

Один класс на все такие источники: адрес, имя источника и наборы (sets)
приходят из ``config/harvest.yaml``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any
from xml.etree import ElementTree

from geonexa_proxima.collectors.base import AsyncHTTPProvider, in_window, parse_date
from geonexa_proxima.domain import Author, CollectedItem, ItemKind, SourceName

_OAI = "http://www.openarchives.org/OAI/2.0/"
_DC = "http://purl.org/dc/elements/1.1/"
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)

#: Сколько страниц ListRecords забирать за окно. КиберЛенинка без набора
#: отдаёт все свои дисциплины — по сотне записей на страницу, и без потолка
#: сутки превращались бы в сотни запросов. Потолок задаётся в конфиге.
DEFAULT_MAX_PAGES = 20

#: Пауза между страницами: у КиберЛенинки есть антибот, и частые запросы с
#: одного адреса заканчиваются капчей вместо XML.
PAGE_PAUSE_SECONDS = 1.0


class OAIError(RuntimeError):
    """Источник ответил не по протоколу — обычно это капча или HTML-заглушка."""


class OAICollector(AsyncHTTPProvider):
    def __init__(
        self,
        base_url: str,
        *,
        source: SourceName = SourceName.OAI,
        sets: Sequence[str] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        metadata_prefix: str = "oai_dc",
        email: str | None = None,
        keep: Callable[[CollectedItem], bool] | None = None,
        **kwargs: object,
    ) -> None:
        contact = f"mailto:{email}" if email else "research harvester"
        kwargs.setdefault("user_agent", f"GeoNexa-Proxima/0.1 ({contact})")
        super().__init__(**kwargs)
        self.base_url = base_url.rstrip("?&")
        self.source = source
        self.sets = [item for item in (sets or ()) if item.strip()]
        self.max_pages = max(1, max_pages)
        self.metadata_prefix = metadata_prefix
        # Читается курсорами и отчётом: у OAI «запрос» — это адрес архива.
        self.query = self.base_url
        # Предфильтр — тот же гейт, что стоит в конвейере. Поиска по словам у
        # OAI нет: за сутки КиберЛенинка отдаёт все дисциплины, и без отсева
        # здесь лимит материалов на источник забивали бы педагогика с
        # медициной, а геотехника оставалась бы за потолком страниц.
        self.keep = keep

    #: Постраничный обход есть, потолок — число страниц, а не записей.
    page_limit = 10**9

    async def collect(
        self, since: datetime, limit: int, until: datetime | None = None
    ) -> list[CollectedItem]:
        seen: dict[str, CollectedItem] = {}
        for set_spec in self.sets or [None]:
            async for record in self._records(since, until, set_spec):
                try:
                    item = self._to_item(record)
                except ValueError:
                    # Кривая запись — например, «идентификатор» с пробелами
                    # вместо адреса — не должна ронять всю страницу.
                    continue
                if item is None:
                    continue
                # Датой у OAI служит datestamp записи — момент правки, а не
                # выхода. Окно по нему уже применил источник; по дате
                # публикации отсеиваем только явно старое, чтобы переиндексация
                # архива не выглядела как новые статьи.
                if item.publication_date is not None and not in_window(
                    item.publication_date, since - timedelta(days=365), None
                ):
                    continue
                if self.keep is not None and not self.keep(item):
                    continue
                seen.setdefault(item.external_id, item)
                if len(seen) >= limit:
                    return list(seen.values())
        return list(seen.values())

    async def _records(self, since: datetime, until: datetime | None, set_spec: str | None):
        params: dict[str, Any] = {
            "verb": "ListRecords",
            "metadataPrefix": self.metadata_prefix,
            "from": since.date().isoformat(),
        }
        if until is not None:
            params["until"] = (until.date() - timedelta(days=1)).isoformat()
        if set_spec:
            params["set"] = set_spec
        pages = 0
        while True:
            root = await self._page(params)
            error = root.find(f"{{{_OAI}}}error")
            if error is not None:
                if error.attrib.get("code") == "noRecordsMatch":
                    return
                raise OAIError(f"{error.attrib.get('code')}: {(error.text or '').strip()}")
            for record in root.iter(f"{{{_OAI}}}record"):
                yield record
            pages += 1
            token = root.find(f".//{{{_OAI}}}resumptionToken")
            if token is None or not (token.text or "").strip() or pages >= self.max_pages:
                return
            await asyncio.sleep(PAGE_PAUSE_SECONDS)
            params = {"verb": "ListRecords", "resumptionToken": token.text.strip()}

    async def _page(self, params: dict[str, Any]) -> ElementTree.Element:
        response = await self._request(
            "GET", self.base_url, params=params, headers={"Accept": "text/xml, application/xml"}
        )
        body = response.content
        head = body[:300].decode("utf-8", "replace").strip()
        lowered = head.lower()
        captcha = "<html" in lowered or "captcha" in lowered or "капч" in lowered
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as error:
            hint = "похоже на капчу или HTML-заглушку вместо XML" if captcha else "не XML"
            raise OAIError(f"{hint}: {head[:120]!r}") from error
        if root.tag != f"{{{_OAI}}}OAI-PMH":
            # HTML-страница — тоже валидный XML, если аккуратно свёрстана.
            # Капча КиберЛенинки именно такая: без этой проверки она читалась
            # бы как «записей нет», и источник молчал бы без единой ошибки.
            hint = "похоже на капчу вместо XML" if captcha else "ответ не по протоколу OAI-PMH"
            raise OAIError(f"{hint}: {head[:120]!r}")
        return root

    def _to_item(self, record: ElementTree.Element) -> CollectedItem | None:
        header = record.find(f"{{{_OAI}}}header")
        if header is None or header.attrib.get("status") == "deleted":
            return None
        identifier = (header.findtext(f"{{{_OAI}}}identifier") or "").strip()
        dc = record.find(f".//{{{_DC}}}title/..")
        if dc is None:
            return None
        titles = _texts(dc, "title")
        if not titles:
            return None
        descriptions = _texts(dc, "description")
        identifiers = _texts(dc, "identifier")
        url = next((value for value in identifiers if value.startswith("http")), None)
        doi = next(
            (match.group(0).lower() for value in identifiers if (match := _DOI.search(value))),
            None,
        )
        dates = [parse_date(value) for value in _texts(dc, "date")]
        published = next((value for value in dates if value is not None), None)
        languages = _texts(dc, "language")
        sources = _texts(dc, "source")
        return CollectedItem(
            source=self.source,
            external_id=identifier or url or doi or titles[0],
            kind=ItemKind.PAPER,
            # Русский и английский заголовки идут отдельными dc:title —
            # склеиваем, чтобы гейт видел оба написания.
            title=" / ".join(dict.fromkeys(titles)),
            abstract="\n\n".join(dict.fromkeys(descriptions)) or None,
            authors=[Author(name=name) for name in _texts(dc, "creator")],
            keywords=_texts(dc, "subject"),
            doi=doi,
            publication_date=published,
            venue=sources[0] if sources else None,
            url=url,
            language=(languages[0][:8] if languages else None),
            raw={"oai": ElementTree.tostring(record, encoding="unicode")},
        )


def _texts(node: ElementTree.Element, name: str) -> list[str]:
    return [
        " ".join(child.text.split())
        for child in node.findall(f"{{{_DC}}}{name}")
        if child.text and child.text.strip()
    ]
