# Gaeclip MONO image: API + worker + frontend in one container.
# (redis stays a separate service — see docker-compose*.yml.)
# Replaces the old api/worker/frontend split images.

FROM node:22-slim AS frontend-builder
WORKDIR /build
# NEXT_PUBLIC_* vars are inlined by Next.js at build time (not runtime),
# so the API base must arrive as a build arg. Compose passes
# NEXT_PUBLIC_API_BASE_URL through (default: the public domain).
ARG NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core libgl1 libegl1 libgles2 bash \
    && rm -rf /var/lib/apt/lists/*
# node runtime for the bundled Next.js standalone server (glibc-compatible).
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
# Deno powers yt-dlp's YouTube JS-challenge solver (distro nodejs is too
# old for it; single static binary via stdlib, no apt repo needed).
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/denoland/deno/releases/download/v2.5.4/deno-x86_64-unknown-linux-gnu.zip','/tmp/deno.zip')" \
  && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
  && chmod +x /usr/local/bin/deno \
  && rm /tmp/deno.zip && deno --version

WORKDIR /app
# Python backend (API + Celery worker + ML stack).
COPY pyproject.toml README.md ./
COPY src ./src
COPY models/blaze_face_short_range.tflite models/yolo26n-face.pt models/pretrain_TalkSet.model ./models/
RUN pip install --no-cache-dir -e ".[api,llm,transcription,framing]" \
  && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
# Next.js standalone server (cwd at runtime: /app/frontend).
COPY --from=frontend-builder /build/.next/standalone ./frontend
COPY --from=frontend-builder /build/.next/static ./frontend/.next/static
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

EXPOSE 8000 3000
# NOTE: externally published as host 4114->8000 (API) and 4113->3000 (UI)
# on 0.0.0.0 — see docker-compose.yml.
CMD ["./docker-entrypoint.sh"]
