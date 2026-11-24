# Qwen Audio Agent build materials

This directory supplements the repository with the environment materials needed to rebuild the Qwen Audio Agent image. The Qwen source checkout is intentionally kept outside this directory; provide the clean checkout separately when running the build.

The bundle contains no API keys. Runtime provider credentials must be supplied through environment variables or a private configuration file.

## Build

Requirements: Docker with BuildKit, Python 3.12 or newer, a clean Qwen Audio Agent checkout, and network access to the public base images and package indexes.

```bash
python3 requirements/build_image.py \
  /path/to/qwen-audio-agent \
  --tag ghcr.io/YOUR_GITHUB_OWNER/qwen-audio-agent:0.5.4-scenario-clock
```

The build script:

1. checks that the Qwen checkout is clean and records its revision and source archive hash;
2. copies `requirements/bridge/` into a temporary Docker build context;
3. downloads the pinned speech assets through `requirements/prepare_assets.py` and verifies SHA256 values;
4. builds `requirements/Dockerfile`; and
5. runs the image contract tests before returning the image ID.

The Qwen source revision used for the current scenario-clock image is recorded in `qwen-source-revision.txt`. The source itself is not duplicated here.

To publish the result to GHCR:

```bash
echo "$CR_PAT" | docker login ghcr.io -u YOUR_GITHUB_OWNER --password-stdin
docker push ghcr.io/YOUR_GITHUB_OWNER/qwen-audio-agent:0.5.4-scenario-clock
```

After publishing, use `bridge/config.gaia2.unified.public.example.json` as the configuration template and replace `YOUR_GITHUB_OWNER`. Never commit the login token or provider API keys.

## Files

- `Dockerfile`: full build from public `gcc`, `node`, and `python` base images. The private Harbor incremental Dockerfile is not used here.
- `build_image.py`: reproducible build helper; it expects the Qwen source path as its positional argument.
- `bridge/`: tau bridge source, npm lock, Python lock, tests, and configuration examples.
- `node-package.json` and `node-package-lock.json`: copies of the bridge npm dependency declarations and lock.
- `python-requirements.in` and `python-requirements.lock`: copies of the speech runtime dependency declarations and lock.
- `prepare_assets.py`: downloads and verifies public VAD, turn detector, NLTK, and speech assets.
- `qwen-source-revision.txt`: source repository, commit, parent commit, and archive hash for provenance.
- `testdata/`: generic public audio fixtures used by image verification.

The lock files at the top level are convenient reference copies. The build uses the matching copies under `bridge/`, so changes to either location should be kept synchronized.

## Reproducibility limits

The application lock files and downloaded asset bytes are pinned. The current Dockerfile still uses public base-image tags, live apt indexes, and live package indexes for convenient rebuilding. Pin those to image digests and package snapshots if bit-for-bit image reproducibility is required.

Before publishing this directory, review the Qwen and third-party licenses and notices.
