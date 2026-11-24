FROM gcc:13 AS confinement
COPY bridge/isolate.c /src/isolate.c
RUN gcc -static -O2 -Wall -Wextra -Werror -o /tau-isolate /src/isolate.c

FROM node:24-bookworm-slim AS native
ARG QWEN_CODE_VERSION=0.26.0
ARG QWEN_SOURCE_REVISION
ARG QWEN_SOURCE_SHA256
LABEL org.tau.qwen-agent.protocol="1" \
      org.tau.qwen-agent.persistent-observer="1" \
      org.tau.qwen-agent.domains="retail,gaia2" \
      org.tau.qwen-agent.confinement="mount-namespace-v1" \
      org.tau.qwen-agent.source-revision=$QWEN_SOURCE_REVISION \
      org.tau.qwen-agent.source-sha256=$QWEN_SOURCE_SHA256

# The build script supplies a clean git archive, never the host's runtime or keys.
COPY qwen-source/ /opt/qwen-audio-agent/
WORKDIR /opt/qwen-audio-agent
RUN npm ci --omit=dev --ignore-scripts --workspace @qwen-audio-agent/server \
    && npm install --global --ignore-scripts @qwen-code/qwen-code@${QWEN_CODE_VERSION}

COPY bridge/ /opt/tau-bridge/
COPY --from=confinement /tau-isolate /opt/tau-bridge/tau-isolate
COPY provenance.json /opt/provenance.json
WORKDIR /opt/tau-bridge
RUN npm ci --omit=dev --ignore-scripts \
    && TAU_QWEN_TEST_ROOT=/opt/qwen-audio-agent TAU_QWEN_CODE_TEST=1 node --test --test-concurrency=1 test/*.test.mjs

FROM python:3.12-slim-bookworm
ARG QWEN_SOURCE_REVISION
ARG QWEN_SOURCE_SHA256
LABEL org.tau.qwen-agent.protocol="1" \
      org.tau.qwen-agent.domains="retail,gaia2" \
      org.tau.qwen-agent.confinement="mount-namespace-v1" \
      org.tau.qwen-agent.source-revision=$QWEN_SOURCE_REVISION \
      org.tau.qwen-agent.source-sha256=$QWEN_SOURCE_SHA256 \
      org.tau.speech-to-speech="1.1.0" \
      org.tau.speech-llm-fallback="1" \
      org.tau.speech-asr.qwen="1"
ENV PYTHONNOUSERSITE=True
RUN sed -i 's|http://deb.debian.org|https://mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=1 -o Acquire::https::Timeout=20 update \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=1 -o Acquire::https::Timeout=20 install -y --no-install-recommends \
      libgomp1 libstdc++6 libsndfile1 libportaudio2 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 node && useradd --uid 1000 --gid 1000 --create-home node
COPY --from=native /usr/local/ /usr/local/
COPY bridge/speech/requirements.lock /opt/s2s-requirements.lock
COPY speech-assets/ /opt/s2s-assets/
RUN --mount=type=cache,target=/root/.cache/uv python -m venv /opt/s2s \
    && /opt/s2s-assets/uv pip sync --python /opt/s2s/bin/python \
      --index-url https://pypi.mirrors.ustc.edu.cn/simple --extra-index-url https://download.pytorch.org/whl/cpu \
      --index-strategy unsafe-best-match /opt/s2s-requirements.lock \
    && /opt/s2s-assets/uv pip install --python /opt/s2s/bin/python --no-deps /opt/s2s-assets/speech_to_speech-1.1.0-py3-none-any.whl
COPY --from=native /opt/ /opt/
RUN PYTHONPATH=/opt/tau-bridge/speech TORCH_HOME=/opt/s2s-assets/torch NLTK_DATA=/opt/s2s-assets/nltk \
      HF_HUB_OFFLINE=1 /opt/s2s/bin/python /opt/tau-bridge/speech/check_install.py
USER node
WORKDIR /opt/tau-bridge
EXPOSE 8787
ENTRYPOINT ["node", "/opt/tau-bridge/bootstrap.mjs"]
