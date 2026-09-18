# MRP Reconciliation - container image.
#
# Runs as its own isolated container, independent of anything else on the
# host: its own process, its own port mapping, no shared volumes or networks
# unless you add them yourself.

FROM python:3.12-slim

# Keeps pip's own cache out of the image and stdout unbuffered in logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer is reused when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py mrp_core.py ./
COPY static ./static

# Not published from this box's port range by default; bound only via
# `docker run -p` at deploy time, so it never collides with another
# container's port just by existing.
EXPOSE 8765

# A non-root user - this container never needs root once it is running.
RUN useradd --create-home --uid 10001 mrpapp
USER mrpapp

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/api/mode', timeout=3).status in (200,401) else 1)"

# Single worker: uploads and run results live in that worker's memory, so a
# second worker would answer requests that never saw the file you uploaded.
CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 4 --timeout 600 --bind 0.0.0.0:${PORT:-8765}"]
