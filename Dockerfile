FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
COPY prompts ./prompts
COPY configs ./configs

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[mysql]"

ENTRYPOINT ["finevision-to-sharegpt"]
CMD ["--help"]
