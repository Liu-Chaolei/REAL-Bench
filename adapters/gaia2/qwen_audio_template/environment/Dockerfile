FROM node:24.19.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl git ripgrep ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install -g opencode-ai@1.18.5
WORKDIR /opt/qwen
COPY . .
RUN npm ci --ignore-scripts --omit=dev
ENV QWAUDIO_CONFIG_DIR=/state/qwen \
    XDG_CONFIG_HOME=/state/config \
    XDG_DATA_HOME=/state/data \
    QWEN_AUDIO_AGENT_OPENCODE_XDG_CONFIG_HOME=/state/opencode \
    QWEN_AUDIO_AGENT_COMPUTER_USE=false \
    QWEN_AUDIO_MEMORY_AUTO=off \
    QWEN_AUDIO_WAKE_WORD_ENABLED=false \
    QWEN_AUDIO_DESKTOP_AUTO_HIDE_SECONDS=0 \
    OPENCODE_RUNTIME=installed \
    AGENT_PROTOCOL=opencode \
    HOST=0.0.0.0 \
    PORT=3101
RUN mkdir -p /state /app /run/evaluation
WORKDIR /app
ENTRYPOINT []
CMD ["sleep", "infinity"]
