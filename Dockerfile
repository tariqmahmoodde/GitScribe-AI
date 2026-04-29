# ---------------------------------------------------------------------------
# GitScribe AI — Dockerfile
# Builds a minimal, reproducible container for the GitHub Action runner.
# ---------------------------------------------------------------------------

# ---- base image -----------------------------------------------------------
FROM python:3.12-slim

# Keeps Python from writing .pyc files and enables unbuffered stdout/stderr
# so log lines appear immediately in the Actions UI.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---- system dependencies --------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

# ---- working directory ----------------------------------------------------
WORKDIR /app

# ---- Python dependencies --------------------------------------------------
# Copy requirements first to leverage Docker layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- application code -----------------------------------------------------
COPY generate_docs.py .

# ---- GitHub Actions sets GITHUB_WORKSPACE as the checkout path -----------
# We default it here so the script also works outside of Actions for testing.
ENV GITHUB_WORKSPACE=/github/workspace

# ---- entrypoint -----------------------------------------------------------
ENTRYPOINT ["python", "/app/generate_docs.py"]
