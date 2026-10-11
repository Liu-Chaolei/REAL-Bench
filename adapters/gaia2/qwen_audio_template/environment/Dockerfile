FROM hearme/qwen-audio-agent-base:1.10.0-local
WORKDIR /opt/qwen
COPY . .
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
RUN mkdir -p /state/backend/workspace /app /run/evaluation /logs/agent \
    && chmod -R 0777 /state /app /run/evaluation /logs
WORKDIR /app
ENTRYPOINT []
CMD ["sleep", "infinity"]
