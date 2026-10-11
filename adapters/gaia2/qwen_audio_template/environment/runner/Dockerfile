FROM hearme/gaia2-are-runner:1.2.0
RUN mkdir -p /evaluation/results && chmod -R 0777 /evaluation
WORKDIR /evaluation
COPY . /opt/runner
ENV PYTHONUNBUFFERED=1 DEMO_FS_PATH=/opt/gaia2-filesystem/demo_filesystem
ENTRYPOINT []
HEALTHCHECK CMD kill -0 1
