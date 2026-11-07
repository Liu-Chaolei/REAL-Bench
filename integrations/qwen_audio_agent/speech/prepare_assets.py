"""Fetch hash-pinned public assets once, then assemble an offline image directory."""

import argparse
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ASSETS = {
    "uv": (
        "d3dc3ca8e29337dd602a9a4df9e6edb15ebc52aed91461debeedb96939dc4ce0",
        [
            "https://files.pythonhosted.org/packages/9b/83/a0bdf4abf86ede79b427778fe27e2b4a022c98a7a8ea1745dcd6c6561f17/uv-0.9.5-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
        ],
    ),
    "speech_to_speech-1.1.0-py3-none-any.whl": (
        "6db4d3e2a6879d9fd8b5bd59b89eced9939451669c8b4d6f4c049eb9b403b372",
        [
            "https://files.pythonhosted.org/packages/b3/f1/96cb85ecbb1228847869f85884bb779b4368ff93a737cb7ad51ba6c4f2c2/speech_to_speech-1.1.0-py3-none-any.whl"
        ],
    ),
    "silero_vad-6.2.3.tar.gz": (
        "d7a23b09202be4953915cb9173089bb307eed0418a9bd4794d9ec5af07fa1996",
        [
            "https://files.pythonhosted.org/packages/32/17/df6bfc8f659fc5c1e082c18275be67c5a581bd2d07734a65b141fc983ccf/silero_vad-6.2.3.tar.gz"
        ],
    ),
    "punkt_tab.zip": (
        "e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106",
        [
            "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip",
            "https://cdn.jsdelivr.net/gh/nltk/nltk_data@gh-pages/packages/tokenizers/punkt_tab.zip",
        ],
    ),
    "averaged_perceptron_tagger_eng.zip": (
        "6025f530624335c67d6547d44757b357b4e79bae030a0383e9887a92c1718f0b",
        [
            "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/taggers/averaged_perceptron_tagger_eng.zip",
            "https://cdn.jsdelivr.net/gh/nltk/nltk_data@gh-pages/packages/taggers/averaged_perceptron_tagger_eng.zip",
        ],
    ),
    "smart-turn-v3.2-cpu.onnx": (
        "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
        [
            "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx",
            "https://hf-mirror.com/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx",
        ],
    ),
}


def prepare(destination: Path, cache: Path):
    """Verify every cached/downloaded byte before copying executable code or models."""
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    for name, (digest, urls) in ASSETS.items():
        path = cache / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            for url in urls:
                try:
                    request = urllib.request.Request(
                        url, headers={"User-Agent": "tau-qwen-speech-build"}
                    )
                    with urllib.request.urlopen(request, timeout=25) as response:
                        content = response.read()
                    if name == "uv":
                        if (
                            hashlib.sha256(content).hexdigest()
                            != "6507bbbcd788553ec4ad5a96fa19364dc0f58b023e31d79868773559a83ec181"
                        ):
                            raise ValueError("uv wheel checksum mismatch")
                        with zipfile.ZipFile(io.BytesIO(content)) as wheel:
                            binary = next(
                                p for p in wheel.namelist() if p.endswith("/uv")
                            )
                            content = wheel.read(binary)
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError("Asset checksum mismatch")
                    path.write_bytes(content)
                    break
                except (OSError, ValueError):
                    continue
            else:
                raise RuntimeError(f"Could not fetch the pinned asset {name}")
        print(f"Verified speech asset: {name}", flush=True)
    hub = destination / "torch/hub"
    hub.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        with tarfile.open(cache / "silero_vad-6.2.3.tar.gz") as archive:
            archive.extractall(temporary, filter="data")
        shutil.copytree(
            Path(temporary) / "silero_vad-6.2.3", hub / "snakers4_silero-vad_master"
        )
    (hub / "trusted_list").write_text("snakers4_silero-vad\n")
    for name, category in [
        ("punkt_tab.zip", "tokenizers"),
        ("averaged_perceptron_tagger_eng.zip", "taggers"),
    ]:
        folder = destination / "nltk" / category
        folder.mkdir(parents=True)
        with zipfile.ZipFile(cache / name) as archive:
            archive.extractall(folder)
    for name in ["smart-turn-v3.2-cpu.onnx", "speech_to_speech-1.1.0-py3-none-any.whl"]:
        shutil.copyfile(cache / name, destination / name)
    shutil.copyfile(cache / "uv", destination / "uv")
    (destination / "uv").chmod(0o755)
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                name: {"sha256": digest, "sources": urls}
                for name, (digest, urls) in ASSETS.items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(tempfile.gettempdir()) / "tau-qwen-speech-downloads",
    )
    args = parser.parse_args()
    prepare(args.destination, args.cache)
