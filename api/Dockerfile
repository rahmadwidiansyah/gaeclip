FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core nodejs && rm -rf /var/lib/apt/lists/*
# Deno powers yt-dlp's YouTube JS-challenge solver (see worker/Dockerfile).
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/denoland/deno/releases/download/v2.5.4/deno-x86_64-unknown-linux-gnu.zip','/tmp/deno.zip')" \
 && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
 && chmod +x /usr/local/bin/deno \
 && rm /tmp/deno.zip && deno --version
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[api,llm]"
CMD ["uvicorn", "gaeclip.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
