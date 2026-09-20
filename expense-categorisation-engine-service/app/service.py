from app.llm import LLMProvider
from app.merchant import normalize_merchant_name
from app.models import BatchRequest, BatchResponse, Category, CategorisationResult, FeedbackRequest, FeedbackResponse
from app.repository import CacheRepository


class CategorisationService:
    def __init__(self, cache: CacheRepository, llm: LLMProvider) -> None:
        self.cache = cache
        self.llm = llm

    def categorise(self, request: BatchRequest) -> BatchResponse:
        results: list[CategorisationResult] = []
        misses = []
        for transaction in request.transactions:
            normalized_name = normalize_merchant_name(transaction.merchant_name)
            cached = self.cache.find(normalized_name)
            if cached:
                results.append(CategorisationResult(
                    transaction_id=transaction.transaction_id,
                    predicted_category=cached.category,
                    confidence_score=1.0,
                    source="cache",
                ))
            else:
                misses.append(transaction.model_copy(update={"merchant_name": normalized_name}))

        if misses:
            try:
                predictions = self.llm.classify(misses)
            except Exception:
                predictions = {}
            for transaction in misses:
                prediction = predictions.get(str(transaction.transaction_id))
                results.append(CategorisationResult(
                    transaction_id=transaction.transaction_id,
                    predicted_category=prediction.category if prediction else Category.UNCATEGORIZED,
                    confidence_score=prediction.confidence if prediction else 0.0,
                    source="llm" if prediction else "fallback",
                    reasoning=prediction.reasoning if prediction else "Unable to obtain a confident classification.",
                ))

        by_id = {str(result.transaction_id): result for result in results}
        return BatchResponse(results=[by_id[str(item.transaction_id)] for item in request.transactions])

    def record_feedback(self, request: FeedbackRequest) -> FeedbackResponse:
        normalized_name = normalize_merchant_name(request.merchant_name)
        updated = self.cache.upsert(normalized_name, request.category_final, request.override_previous)
        message = f"Cache rule successfully updated for {normalized_name}" if updated else "Existing cache rule preserved"
        return FeedbackResponse(success=True, cache_updated=updated, message=message)
