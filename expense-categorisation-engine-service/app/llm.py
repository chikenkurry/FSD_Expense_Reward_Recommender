from dataclasses import dataclass
import random
import time
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


def _is_transient_error(error: Exception) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True

    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    return status_code == 429 or status_code in {500, 502, 503, 504}


class RetryingLLMProvider:
    def __init__(self, provider: LLMProvider, max_retries: int, base_delay_seconds: float) -> None:
        self.provider = provider
        self.max_retries = max(0, max_retries)
        self.base_delay_seconds = max(0.0, base_delay_seconds)

    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]:
        for attempt in range(self.max_retries + 1):
            try:
                return self.provider.classify(transactions)
            except Exception as error:
                if not _is_transient_error(error) or attempt == self.max_retries:
                    raise
                delay = self.base_delay_seconds * (2**attempt)
                delay += random.uniform(0, self.base_delay_seconds * 0.25)
                time.sleep(delay)
        raise RuntimeError("LLM retry loop exited unexpectedly")


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
