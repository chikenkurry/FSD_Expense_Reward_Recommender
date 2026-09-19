from fastapi import FastAPI

from app.config import get_settings
from app.llm import UnconfiguredLLMProvider
from app.models import BatchRequest, BatchResponse, FeedbackRequest, FeedbackResponse
from app.repository import CacheRepository
from app.service import CategorisationService


def create_app() -> FastAPI:
    settings = get_settings()
    service = CategorisationService(
        cache=CacheRepository(settings.database_path),
        llm=UnconfiguredLLMProvider(),
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


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
