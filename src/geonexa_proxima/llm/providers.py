"""Grounded structured-output clients for OpenAI-compatible chat APIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from geonexa_proxima.collectors.base import AsyncHTTPProvider, as_dict, as_list
from geonexa_proxima.config import Settings
from geonexa_proxima.domain import CollectedItem, DeepAnalysis, RankResult, StoredItem

_GROUNDING = (
    "Use only facts explicitly present in the supplied source record. Never invent metrics, "
    "results, datasets, affiliations, code availability, or prior art. Mark unavailable facts "
    "as unknown using null, false, an empty list, or a short 'Not stated' phrase as allowed by "
    "the schema. Return only JSON matching the supplied schema."
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class OpenAICompatibleJSONClient(AsyncHTTPProvider):
    """Minimal async chat client with JSON Schema output and parsing recovery."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        **kwargs: object,
    ) -> None:
        super().__init__(timeout=timeout, **kwargs)
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.api_key = api_key
        self.model = model

    async def generate(
        self,
        output_type: type[ModelT],
        *,
        system: str,
        user: str,
    ) -> ModelT:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{system}\n\n{_GROUNDING}"},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for parse_attempt in range(2):
            payload = {
                "model": self.model,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": output_type.__name__,
                        "strict": True,
                        "schema": output_type.model_json_schema(),
                    },
                },
            }
            try:
                response = await self._request(
                    "POST",
                    self.url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code not in {400, 404, 422}:
                    raise
                payload["response_format"] = {"type": "json_object"}
                response = await self._request(
                    "POST",
                    self.url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            content = _response_content(response.json())
            try:
                return _validate_output(output_type, content)
            except (json.JSONDecodeError, ValidationError, ValueError) as error:
                last_error = error
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
        raise ValueError(f"LLM did not return valid {output_type.__name__} JSON") from last_error


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
        retries=settings.llm_max_retries,
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
    )


def create_ranker(settings: Settings) -> LLMRanker:
    return LLMRanker(_client(settings, heavy=False))


def create_analyzer(settings: Settings) -> LLMAnalyzer:
    return LLMAnalyzer(_client(settings, heavy=True))


def create_personalizer(settings: Settings) -> LLMProfileExplainer:
    return LLMProfileExplainer(_client(settings, heavy=False))


def create_deep_personalizer(settings: Settings) -> LLMProfileExplainer:
    return LLMProfileExplainer(_client(settings, heavy=True))


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
