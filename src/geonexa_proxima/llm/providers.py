"""Grounded structured-output clients for OpenAI-compatible chat APIs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from geonexa_proxima.collectors.base import AsyncHTTPProvider, as_dict, as_list
from geonexa_proxima.config import Settings
from geonexa_proxima.domain import CollectedItem, DeepAnalysis, RankResult, StoredItem
from geonexa_proxima.services.translation import LLMTranslator

_GROUNDING = (
    "Use only facts explicitly present in the supplied source record. Never invent metrics, "
    "results, datasets, affiliations, code availability, or prior art. Mark unavailable facts "
    "as unknown using null, false, an empty list, or a short 'Not stated' phrase as allowed by "
    "the schema. Return only JSON matching the supplied schema."
)

ModelT = TypeVar("ModelT", bound=BaseModel)

logger = logging.getLogger("geonexa.llm")


class OpenAICompatibleJSONClient(AsyncHTTPProvider):
    """Minimal async chat client with JSON Schema output and parsing recovery."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(timeout=timeout, **kwargs)
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key = api_key
        self.model = model
        # Раньше эти три настройки читались из `.env`, сидировались в реестр
        # моделей — и ни одна не доезжала до запроса. Провайдер брал свои
        # значения по умолчанию: у DeepSeek это 4096 токенов, и длинный разбор
        # обрезался на середине JSON, а в логе стояло «invalid JSON».
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.json_mode = json_mode

    async def generate(
        self,
        output_type: type[ModelT],
        *,
        system: str,
        user: str,
        grounding: bool = True,
    ) -> ModelT:
        # Напоминание «не выдумывай фактов» нужно оценке и разбору статьи;
        # переводу профиля оно только мешает — там нет «исходной записи».
        # «Return only JSON» остаётся всегда: json_object у DeepSeek требует
        # слова JSON в промпте.
        suffix = _GROUNDING if grounding else "Return only JSON matching the supplied schema."
        messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{system}\n\n{suffix}"},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        content = ""
        for parse_attempt in range(2):
            payload: dict[str, Any] = {"model": self.model, "messages": messages}
            if self.temperature is not None:
                payload["temperature"] = self.temperature
            if self.max_tokens is not None:
                payload["max_tokens"] = self.max_tokens
            if self.json_mode:
                payload["response_format"] = self._response_format(output_type)
            try:
                response = await self._request(
                    "POST",
                    self.url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {400, 404, 422} or not self.json_mode:
                    raise
                # Провайдер не принял json_schema — запоминаем и дальше ходим
                # с json_object, а не пробуем строгую схему на каждом вызове.
                if self._schema_supported is not False:
                    self._schema_supported = False
                    payload["response_format"] = {"type": "json_object"}
                else:
                    payload.pop("response_format", None)
                    self.json_mode = False
                response = await self._request(
                    "POST",
                    self.url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            body = response.json()
            content = _response_content(body)
            finish = _finish_reason(body)
            try:
                return _validate_output(output_type, content)
            except (json.JSONDecodeError, ValidationError, ValueError) as error:
                last_error = error
                logger.warning(
                    "LLM %s: ответ не разобрался как %s (finish_reason=%s, попытка %s): %s. "
                    "Начало ответа: %r",
                    self.model,
                    output_type.__name__,
                    finish,
                    parse_attempt + 1,
                    error,
                    content[:300],
                )
                if parse_attempt == 0:
                    messages.extend(
                        [
                            {"role": "assistant", "content": content},
                            {
                                "role": "user",
                                "content": (
                                    "The prior answer was invalid. Return one corrected JSON "
                                    "object "
                                    "that exactly matches the schema, without markdown."
                                ),
                            },
                        ]
                    )
        raise ValueError(
            f"LLM did not return valid {output_type.__name__} JSON: {last_error}; "
            f"ответ начинался с {content[:160]!r}"
        ) from last_error

    #: None — ещё не знаем, принимает ли провайдер строгую схему.
    _schema_supported: bool | None = None

    def _response_format(self, output_type: type[BaseModel]) -> dict[str, Any]:
        if self._schema_supported is False:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": output_type.__name__,
                "strict": True,
                "schema": output_type.model_json_schema(),
            },
        }


class LLMRanker:
    def __init__(self, client: OpenAICompatibleJSONClient) -> None:
        self.client = client

    async def rank(self, item: CollectedItem, semantic_score: float) -> RankResult:
        source = item.model_dump(mode="json", exclude={"raw"})
        return await self.client.generate(
            RankResult,
            system=(
                "You are a strict scientific triage reviewer for geotechnics, geospatial "
                "engineering, numerical methods, and applied AI. Score every numeric dimension "
                "from 0 to 10. Treat the retrieval similarity as a hint, not as evidence."
            ),
            user=(
                f"Retrieval similarity: {semantic_score:.6f}\n"
                f"Source record:\n{json.dumps(source, ensure_ascii=False)}"
            ),
        )


class LLMAnalyzer:
    def __init__(self, client: OpenAICompatibleJSONClient) -> None:
        self.client = client

    async def analyze(self, item: CollectedItem, rank: RankResult) -> DeepAnalysis:
        source = item.model_dump(mode="json", exclude={"raw"})
        ranking = rank.model_dump(mode="json")
        return await self.client.generate(
            DeepAnalysis,
            system=(
                "You are a careful scientific analyst. Distinguish reported claims from your "
                "assessment, explicitly expose missing evidence, and propose research ideas as "
                "hypotheses rather than claims about the source."
            ),
            user=(
                f"Source record:\n{json.dumps(source, ensure_ascii=False)}\n\n"
                f"Prior triage:\n{json.dumps(ranking, ensure_ascii=False)}"
            ),
        )


class PersonalReason(BaseModel):
    reason: str


class LLMProfileExplainer:
    """Generate a grounded, profile-specific reason without changing global scores."""

    def __init__(self, client: OpenAICompatibleJSONClient) -> None:
        self.client = client

    async def explain(
        self,
        item: StoredItem,
        *,
        profile_text: str,
        personal_score: float,
    ) -> str:
        source = item.model_dump(mode="json", exclude={"raw"})
        result = await self.client.generate(
            PersonalReason,
            system=(
                "Explain briefly why this source is or is not relevant to the supplied personal "
                "research profile. Do not invent claims about the source."
            ),
            user=(
                f"Personal profile:\n{profile_text}\n\n"
                f"Computed personal score: {personal_score:.4f}\n"
                f"Source record:\n{json.dumps(source, ensure_ascii=False)}"
            ),
        )
        return result.reason


@dataclass(slots=True)
class LLMProviders:
    ranker: LLMRanker
    analyzer: LLMAnalyzer
    personalizer: LLMProfileExplainer
    deep_personalizer: LLMProfileExplainer
    light_client: OpenAICompatibleJSONClient
    heavy_client: OpenAICompatibleJSONClient
    translator: LLMTranslator | None = None

    async def aclose(self) -> None:
        await self.light_client.aclose()
        await self.heavy_client.aclose()


def _client(settings: Settings, *, heavy: bool) -> OpenAICompatibleJSONClient:
    """Собрать клиент из настроек.

    Раньше параметры перечислялись в четырёх местах, и `LLM_MAX_RETRIES`
    не попал ни в одно из них: настройка была, а повторов при 429 и 5xx
    столько, сколько зашито в базовом классе.
    """

    return OpenAICompatibleJSONClient(
        base_url=settings.heavy_llm_base_url if heavy else settings.light_llm_base_url,
        api_key=settings.llm_api_key(heavy=heavy),
        model=settings.heavy_llm_model if heavy else settings.light_llm_model,
        timeout=settings.llm_timeout_seconds,
        retries=max(1, settings.llm_max_retries),
        temperature=settings.heavy_llm_temperature if heavy else settings.light_llm_temperature,
        max_tokens=settings.heavy_llm_max_tokens if heavy else settings.light_llm_max_tokens,
        json_mode=settings.heavy_llm_json_mode if heavy else settings.light_llm_json_mode,
    )


def create_llm_providers(settings: Settings) -> LLMProviders:
    light = _client(settings, heavy=False)
    heavy = _client(settings, heavy=True)
    return LLMProviders(
        ranker=LLMRanker(light),
        analyzer=LLMAnalyzer(heavy),
        personalizer=LLMProfileExplainer(light),
        deep_personalizer=LLMProfileExplainer(heavy),
        light_client=light,
        heavy_client=heavy,
        translator=LLMTranslator(light),
    )


def create_ranker(settings: Settings) -> LLMRanker:
    return LLMRanker(_client(settings, heavy=False))


def create_analyzer(settings: Settings) -> LLMAnalyzer:
    return LLMAnalyzer(_client(settings, heavy=True))


def create_personalizer(settings: Settings) -> LLMProfileExplainer:
    return LLMProfileExplainer(_client(settings, heavy=False))


def create_deep_personalizer(settings: Settings) -> LLMProfileExplainer:
    return LLMProfileExplainer(_client(settings, heavy=True))


def _finish_reason(payload: object) -> str | None:
    choices = as_list(as_dict(payload).get("choices"))
    if not choices:
        return None
    reason = as_dict(choices[0]).get("finish_reason")
    return str(reason) if reason is not None else None


def _response_content(payload: object) -> str:
    body = as_dict(payload)
    choices = as_list(body.get("choices"))
    if not choices:
        raise ValueError("LLM response contains no choices")
    message = as_dict(as_dict(choices[0]).get("message"))
    parsed = message.get("parsed")
    if isinstance(parsed, dict):
        return json.dumps(parsed)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(as_dict(part).get("text"))
            for part in content
            if as_dict(part).get("text") is not None
        ]
        if texts:
            return "".join(texts)
    tool_calls = as_list(message.get("tool_calls"))
    if tool_calls:
        arguments = as_dict(as_dict(tool_calls[0]).get("function")).get("arguments")
        if isinstance(arguments, str):
            return arguments
    raise ValueError("LLM response has no parseable content")


def _validate_output(output_type: type[ModelT], content: str) -> ModelT:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if isinstance(value, dict):
        for key in (output_type.__name__, output_type.__name__.lower(), "result", "data"):
            nested = value.get(key)
            if isinstance(nested, dict):
                value = nested
                break
    return output_type.model_validate(value)
