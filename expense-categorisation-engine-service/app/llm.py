from dataclasses import dataclass
from typing import Protocol

from app.models import Category, CategorisationTransaction


@dataclass(frozen=True)
class LLMClassification:
    category: Category
    confidence: float
    reasoning: str


class LLMProvider(Protocol):
    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]: ...


class UnconfiguredLLMProvider:
    """Safe local default. A provider adapter can be injected later."""

    def classify(self, transactions: list[CategorisationTransaction]) -> dict[str, LLMClassification]:
        raise RuntimeError("No LLM provider is configured")
