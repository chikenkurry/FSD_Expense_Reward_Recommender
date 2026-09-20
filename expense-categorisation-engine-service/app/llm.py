from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models import Category, CategorisationTransaction


@dataclass(frozen=True)
class LLMClassification:
    category: Category
    confidence: float
    reasoning: str


class LLMProvider(Protocol):
    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]: ...


class LLMClassificationOutput(BaseModel):
    """Provider-independent schema enforced on every LLM response."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class LLMBatchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[LLMClassificationOutput]


def _prompt_for(transactions: list[CategorisationTransaction]) -> str:
    transaction_lines = "\n".join(
        f"- id={item.transaction_id}; merchant={item.merchant_name}; "
        f"amount={item.amount}; currency={item.currency}; raw={item.raw_description or ''}"
        for item in transactions
    )
    categories = ", ".join(category.value for category in Category)
    return (
        "Categorize each financial transaction. Use only one of these categories: "
        f"{categories}. If the merchant is gibberish or the category cannot be determined, "
        "use Uncategorized with low confidence. Return one result for every transaction "
        "using the exact transaction id.\n\nTransactions:\n" + transaction_lines
    )


def _to_classifications(output: LLMBatchOutput) -> dict[str, LLMClassification]:
    return {
        item.transaction_id: LLMClassification(
            category=item.category,
            confidence=item.confidence,
            reasoning=item.reasoning,
        )
        for item in output.results
    }


class OpenAIProvider:
    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]:
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": "You are a careful expense categorization engine."},
                {"role": "user", "content": _prompt_for(transactions)},
            ],
            text_format=LLMBatchOutput,
        )
        if response.output_parsed is None:
            raise ValueError("OpenAI returned no structured categorisation")
        return _to_classifications(LLMBatchOutput.model_validate(response.output_parsed))


class GeminiProvider:
    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model

    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]:
        response = self.client.models.generate_content(
            model=self.model,
            contents=_prompt_for(transactions),
            config={
                "response_mime_type": "application/json",
                "response_schema": LLMBatchOutput,
            },
        )
        if not response.text:
            raise ValueError("Gemini returned no structured categorisation")
        return _to_classifications(LLMBatchOutput.model_validate_json(response.text))


class UnconfiguredLLMProvider:
    """Safe local default. A provider adapter can be injected later."""

    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]:
        raise RuntimeError("No LLM provider is configured")
