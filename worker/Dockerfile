FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core libgl1 libegl1 libgles2 nodejs && rm -rf /var/lib/apt/lists/*
# Deno powers yt-dlp's YouTube JS-challenge solver (needs a modern
# runtime; distro nodejs is too old). Single static binary via stdlib.
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/denoland/deno/releases/download/v2.5.4/deno-x86_64-unknown-linux-gnu.zip','/tmp/deno.zip')" \
 && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
 && chmod +x /usr/local/bin/deno \
 && rm /tmp/deno.zip && deno --version
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY worker/entrypoint.sh ./entrypoint.sh
COPY models/blaze_face_short_range.tflite models/yolo26n-face.pt ./models/
# TalkNet TalkSet weights (MIT, see src/gaeclip/talknet/NOTICE). Needed at
# runtime for active-speaker escalation; heuristic-only mode works without
# torch but WITH torch TalkNet actually runs.
COPY models/TalkNet-ASD/pretrain_TalkSet.model ./models/TalkNet-ASD/
RUN pip install --no-cache-dir -e ".[api,llm,transcription,framing]" \
  && chmod +x ./entrypoint.sh
# CPU-only torch for TalkNet (CUDA images stay lean on purpose; a CUDA
# build would pull the ~800MB+ default wheel). Pinned to the CPU index.
RUN pip install --no-cache-dir torch \
  --index-url https://download.pytorch.org/whl/cpu
CMD ["./entrypoint.sh"]
