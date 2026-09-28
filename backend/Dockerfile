# MonxuPlan API / worker image
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN useradd --create-home --uid 10001 monxu
COPY pyproject.toml ./
COPY monxuplan ./monxuplan
COPY monxuplan_engine ./monxuplan_engine
RUN pip install ".[postgres,redis]" && pip install anthropic
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /app/data && chown -R monxu /app
USER monxu
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)" || exit 1
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "monxuplan.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "2"]
