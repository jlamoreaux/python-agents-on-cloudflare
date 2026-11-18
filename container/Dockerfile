# Built from the repo root (build_context in wrangler.jsonc), so the shared
# agent/ package can be copied in alongside the container code.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY container/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent ./agent
COPY container/server.py container/tools.py ./

# Run as an unprivileged user and make the app read-only, so model-written code
# cannot overwrite the server it runs next to.
RUN useradd --system --no-create-home app && chmod -R a-w /app
USER app

EXPOSE 8080
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
