from typing import Any

import pytest

from geonexa_proxima.domain import ItemKind, StoredItem
from geonexa_proxima.llm.providers import LLMProfileExplainer


class FakeJSONClient:
    def __init__(self) -> None:
        self.user_prompt = ""

    async def generate(self, output_type: type[Any], *, system: str, user: str) -> Any:
        self.user_prompt = user
        assert "invent" in system
        return output_type(reason="The method matches the liquefaction profile.")


@pytest.mark.asyncio
async def test_profile_explainer_includes_profile_and_item() -> None:
    client = FakeJSONClient()
    explainer = LLMProfileExplainer(client)  # type: ignore[arg-type]
    item = StoredItem(kind=ItemKind.PAPER, title="Neural operators for liquefaction")

    reason = await explainer.explain(
        item,
        profile_text="Geotechnical ML and soil liquefaction",
        personal_score=0.91,
    )

    assert reason.startswith("The method")
    assert "Geotechnical ML" in client.user_prompt
    assert item.title in client.user_prompt
