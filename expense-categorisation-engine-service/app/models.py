from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Category(StrEnum):
    DINING = "Dining"
    GROCERIES = "Groceries"
    TRANSPORT = "Transport"
    SHOPPING = "Shopping"
    UTILITIES = "Utilities"
    SUBSCRIPTIONS = "Subscriptions"
    ENTERTAINMENT = "Entertainment"
    INCOME = "Income"
    UNCATEGORIZED = "Uncategorized"


class CategorisationTransaction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    transaction_id: UUID
    merchant_name: str = Field(min_length=1, max_length=255)
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)
    raw_description: str | None = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def currency_is_iso_code(cls, value: str) -> str:
        return value.upper()


class BatchRequest(BaseModel):
    transactions: list[CategorisationTransaction] = Field(min_length=1, max_length=50)


class CategorisationResult(BaseModel):
    transaction_id: UUID
    predicted_category: Category
    confidence_score: float = Field(ge=0.0, le=1.0)
    source: str
    reasoning: str | None = None


class BatchResponse(BaseModel):
    results: list[CategorisationResult]


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    merchant_name: str = Field(min_length=1, max_length=255)
    category_final: Category
    override_previous: bool = False


class FeedbackResponse(BaseModel):
    success: bool
    cache_updated: bool
    message: str
