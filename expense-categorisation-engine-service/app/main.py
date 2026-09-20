from fastapi import FastAPI

from app.config import get_settings
from app.llm import GeminiProvider, OpenAIProvider, RetryingLLMProvider, UnconfiguredLLMProvider
from app.models import BatchRequest, BatchResponse, FeedbackRequest, FeedbackResponse
from app.repository import CacheRepository
from app.service import CategorisationService


def create_app() -> FastAPI:
    settings = get_settings()
    llm = RetryingLLMProvider(
        provider=_build_llm_provider(settings),
        max_retries=settings.llm_max_retries,
        base_delay_seconds=settings.llm_retry_base_delay_seconds,
    )
    service = CategorisationService(
        cache=CacheRepository(settings.database_path),
        llm=llm,
    )
    app = FastAPI(title="Expense Categorisation Engine", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/categorization/batch", response_model=BatchResponse)
    def categorise(request: BatchRequest) -> BatchResponse:
        return service.categorise(request)

    @app.post("/api/categorization/feedback", response_model=FeedbackResponse)
    def feedback(request: FeedbackRequest) -> FeedbackResponse:
        return service.record_feedback(request)

    return app


def _build_llm_provider(settings):
    if settings.llm_provider == "openai":
        if not settings.llm_api_key or not settings.llm_model:
            raise RuntimeError("LLM_API_KEY and LLM_MODEL are required for OpenAI")
        return OpenAIProvider(settings.llm_api_key, settings.llm_model)
    if settings.llm_provider == "gemini":
        if not settings.llm_api_key or not settings.llm_model:
            raise RuntimeError("LLM_API_KEY and LLM_MODEL are required for Gemini")
        return GeminiProvider(settings.llm_api_key, settings.llm_model)
    return UnconfiguredLLMProvider()


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
