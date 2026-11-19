FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m app
USER app
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/app/.local/bin:${PATH}"
WORKDIR /app
COPY --chown=app pyproject.toml ./
COPY --chown=app wallmpop ./wallmpop
RUN pip install --user --no-cache-dir .
ENV DB_PATH=/app/data/wallmpop.db
CMD ["python", "-m", "wallmpop"]
