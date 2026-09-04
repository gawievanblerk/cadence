FROM python:3.12-slim

WORKDIR /app

# The code and the programme ship together. One image is one content version,
# which is what lets a rollback of the image also roll back the treatment.
COPY pyproject.toml README.md ./
COPY src ./src
COPY content ./content
COPY scripts ./scripts

# `postgres` brings the driver; `cryptography` is what makes identity
# encryption production grade (the start-up check warns without it).
RUN pip install --no-cache-dir ".[postgres]" cryptography

# Run as a non-root user. Created after COPY so /app is owned by it, which also
# lets the SQLite default write var/ when no database URL is configured.
RUN useradd --create-home --uid 10001 cadence && chown -R cadence /app
USER cadence

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

CMD ["python", "-m", "uvicorn", "cadence.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
