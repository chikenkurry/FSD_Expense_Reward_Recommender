FROM python:3.11-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY credit_card_service ./credit_card_service
RUN python -m compileall -q credit_card_service
FROM python:3.11-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app /app
USER 10001
ENV CARD_DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2)"
ENTRYPOINT ["python","-m","credit_card_service"]
CMD ["serve","--config","config/sources.example.json","--db","/data/catalogue.sqlite","--host","0.0.0.0"]
