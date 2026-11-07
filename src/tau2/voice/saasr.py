"""Per-call continuous scene playback, independent of asynchronous synthesis."""

import hashlib
import itertools
import json
import shutil
import struct
import threading
import time
from concurrent.futures import CancelledError, TimeoutError
from pathlib import Path

import numpy as np

from tau2.config import DEFAULT_SAASR_CROSSFADE_SECONDS
from tau2.data_model.audio import PCM_SAMPLE_RATE, AudioEncoding, AudioFormat
from tau2.voice.saasr_worker import SaasrWorkerError, get_pool


class WaveTrack:
    """Append PCM16 or IEEE float WAV without a model-runtime dependency."""

    def __init__(self, path, *, floating=False, sample_rate=PCM_SAMPLE_RATE):
        self.file = Path(path).open("w+b")
        self.floating = floating
        self.count = 0
        self.width = 4 if floating else 2
        self.file.write(
            b"RIFF\x00\x00\x00\x00WAVEfmt "
            + struct.pack(
                "<IHHIIHH",
                16,
                3 if floating else 1,
                1,
                sample_rate,
                sample_rate * self.width,
                self.width,
                self.width * 8,
            )
            + b"data\x00\x00\x00\x00"
        )

    def write(self, samples):
        """Write normalized samples, retaining float stems without clipping."""
        data = (
            np.asarray(samples, dtype="<f4").tobytes()
            if self.floating
            else np.rint(np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        )
        self.file.write(data)
        self.count += len(data)

    def close(self):
        """Finalize the WAV header for an ordinary audio player."""
        self.file.seek(4)
        self.file.write(struct.pack("<I", 36 + self.count))
        self.file.seek(40)
        self.file.write(struct.pack("<I", self.count))
        self.file.close()


def turn_seed(seed: int, label: str) -> int:
    """Derive independent seeds without touching a process-global RNG."""
    return int.from_bytes(
        hashlib.sha256(f"{seed}:{label}".encode()).digest()[:8], "big"
    )


def loop_audio(audio, position, count, crossfade):
    """Read a continuous loop, crossfading its seam without altering initial timing."""
    if not len(audio):
        raise ValueError("Cannot loop empty audio")
    fade = min(crossfade, len(audio) // 4)
    positions = np.arange(position, position + count, dtype=np.int64)
    if fade == 0:
        return np.asarray(audio[positions % len(audio)], dtype=np.float64)
    indices = np.where(
        positions < len(audio),
        positions,
        (positions - len(audio)) % (len(audio) - fade) + fade,
    )
    result = np.asarray(audio[indices], dtype=np.float64).copy()
    seam = indices >= len(audio) - fade
    offset = indices[seam] - (len(audio) - fade)
    weight = offset / fade
    result[seam] = result[seam] * (1 - weight) + audio[offset] * weight
    return result


class SaasrSession:
    """One trial's scene, cancellation token, audio cursor and evidence writers."""

    def __init__(self, config, scene_yaml, directory, *, seed, language, identity=None):
        self.config = config
        self.scene_yaml = Path(scene_yaml).resolve()
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.language = language
        self.identity = identity
        self.pool = get_pool(config)
        self.cancelled = threading.Event()
        self.counter = itertools.count()
        self.position = 0
        self.started = False
        self.closed = False
        self.noise_gain = None
        self.events = []
        self.writers = {}
        self.pending_ticks = {}
        self.tick_counter = itertools.count()
        self.sent_samples = 0
        self.sent_scene_samples = 0
        if hasattr(self.pool, "register_session"):
            self.pool.register_session(self)
        try:
            self.prepare()
        except BaseException:
            self.close()
            raise

    def request(self, payload, *, prepare=False):
        """Cancel queued work promptly; collect running work in the owned worker."""
        future = self.pool.submit(
            {
                "scene_yaml": str(self.scene_yaml),
                "model_dir": str(self.config.model_dir),
                "scene_seed": self.seed,
                **payload,
            },
            prepare=prepare,
        )
        while True:
            if self.cancelled.is_set():
                future.cancel()
                raise CancelledError("SAASR trial ended before synthesis delivery")
            try:
                return future.result(timeout=0.1)
            except TimeoutError:
                pass

    def prepare(self):
        """Prepare all background before the remote Agent session is opened."""
        self.scene = self.request(
            {"op": "prepare", "output_dir": str(self.directory / "scene")}, prepare=True
        )
        if self.scene["sample_rate"] != PCM_SAMPLE_RATE:
            raise ValueError("SAASR worker returned an incompatible sample rate")
        directory = Path(self.scene["directory"])
        self.background = np.load(
            directory / "background.npy", mmap_mode="r", allow_pickle=False
        )
        self.active = np.load(
            directory / "active.npy", mmap_mode="r", allow_pickle=False
        )
        self.noise = np.load(directory / "noise.npy", mmap_mode="r", allow_pickle=False)
        for audio in (self.background, self.noise):
            if audio.ndim != 1 or not audio.size or not np.isfinite(audio).all():
                raise ValueError("Invalid prepared SAASR audio")
        self.fade = round(DEFAULT_SAASR_CROSSFADE_SECONDS * PCM_SAMPLE_RATE)
        self.target_peak = 10 ** (self.scene["peak_dbfs"] / 20)
        for name in ("main", "background", "noise", "mixed"):
            self.writers[name] = WaveTrack(
                self.directory / f"played_{name}.wav", floating=name != "mixed"
            )
        self.tick_log = (self.directory / "playback.jsonl").open("w")

    def synthesize(self, text):
        """Return new foreground and source metadata, never advancing playback."""
        index = next(self.counter)
        started = time.perf_counter()
        result = self.request(
            {
                "op": "foreground",
                "text": text,
                "language": self.language,
                "seed": turn_seed(self.seed, f"utterance:{index}"),
                "output_dir": str(self.directory / f"utterance_{index:05d}"),
            }
        )
        wet = np.load(result["path"], allow_pickle=False)
        if (
            wet.ndim != 1
            or not wet.size
            or not np.isfinite(wet).all()
            or not 0 < result["speech_samples"] <= len(wet)
        ):
            raise SaasrWorkerError("Invalid SAASR foreground waveform")
        # Transport PCM is temporary. Restore this gain before the final common
        # mixer gain, so high foreground peaks are not clipped at this boundary.
        transport_gain = min(1.0, 0.95 / max(float(np.max(np.abs(wet))), 1e-9))
        pcm = np.rint(wet * transport_gain * 32767).astype("<i2")
        metadata = {
            **result,
            "transport_gain": transport_gain,
            "render_seconds": time.perf_counter() - started,
            "utterance": index,
        }
        self.events.append({"event": "generated", **metadata})
        return pcm.tobytes(), metadata

    def commit(self, full_message):
        """Calibrate once, at the first accepted utterance; return initial lead-in samples."""
        foreground = full_message.raw_data["saasr_foreground"]
        self.events.append(
            {
                "event": "accepted",
                "utterance": foreground["utterance"],
                "playback_sample": self.position,
            }
        )
        if self.started:
            return 0
        data = (
            np.frombuffer(full_message.get_audio_bytes(), dtype="<i2").astype(
                np.float64
            )
            / 32767
        )
        meta = full_message.raw_data["saasr_foreground"]
        data /= meta["transport_gain"]
        offset = self.scene["initial_offset_samples"]
        length = max(len(self.background), offset + len(data))
        clean = loop_audio(self.background, 0, length, self.fade)
        mask = loop_audio(self.active.astype(np.float32), 0, length, 0) > 0
        clean[offset : offset + len(data)] += data
        mask[offset : offset + meta["speech_samples"]] = True
        noise = loop_audio(self.noise, 0, length, self.fade)
        clean_rms = float(np.sqrt(np.mean(clean[mask] ** 2)))
        noise_rms = float(np.sqrt(np.mean(noise[mask] ** 2)))
        self.noise_gain = (
            (clean_rms / max(noise_rms, 1e-12) / 10 ** (self.scene["snr_db"] / 20))
            if self.scene["snr_db"] is not None
            else 0.0
        )
        self.started = True
        self.events.append(
            {
                "event": "started",
                "noise_gain": self.noise_gain,
                "calibration_snr_db": self.scene["snr_db"],
                "calibration_samples": length,
            }
        )
        return offset

    def mix_tick(self, chunk, count):
        """Mix every emitted tick, even during Agent speech or user inference."""
        main = np.zeros(count, dtype=np.float64)
        if chunk is not None:
            pcm = np.frombuffer(chunk.get_audio_bytes(), dtype="<i2")
            main[: len(pcm)] = pcm / 32767
            meta = (chunk.raw_data or {}).get("saasr_foreground", {})
            main /= meta.get("transport_gain", 1.0)
        background = np.zeros(count)
        noise = np.zeros(count)
        if self.started:
            background = loop_audio(self.background, self.position, count, self.fade)
            noise = (
                loop_audio(self.noise, self.position, count, self.fade)
                * self.noise_gain
            )
        mixed = main + background + noise
        gain = min(1.0, self.target_peak / max(float(np.max(np.abs(mixed))), 1e-12))
        token = next(self.tick_counter)
        self.pending_ticks[token] = {
            "tracks": {
                "main": main * gain,
                "background": background * gain,
                "noise": noise * gain,
                "mixed": mixed * gain,
            },
            "sample": self.position,
            "count": count,
            "started": self.started,
            "gain": gain,
            "main_speech": bool(chunk and chunk.contains_speech),
        }
        if self.started:
            self.position += count
        return np.rint(mixed * gain * 32767).astype("<i2").tobytes(), token

    def record(self):
        """Serializable source and playback evidence for saved trajectories."""
        return {
            "version": "saasr-continuous-v1",
            "scene_yaml": str(self.scene_yaml),
            "seed": self.seed,
            "scene": getattr(self, "scene", None),
            "identity": self.identity,
            "workers": self.pool.identities,
            "directory": str(self.directory),
            "emitted_scene_samples": self.position,
            "played_samples": self.sent_scene_samples,
            "sent_samples": self.sent_samples,
            "noise_gain": self.noise_gain,
            "events": list(self.events),
        }

    def record_sent(self, chunk):
        """Preserve the actual post-codec outgoing samples as playable PCM."""
        import audioop

        if chunk is None:
            return
        token = (chunk.raw_data or {}).get("saasr_output_id")
        if token not in self.pending_ticks:
            return
        frame = self.pending_ticks.pop(token)
        for name, samples in frame.pop("tracks").items():
            self.writers[name].write(samples)
        self.tick_log.write(
            json.dumps({"sent_sample": self.sent_samples, **frame}) + "\n"
        )
        self.sent_samples += frame["count"]
        self.sent_scene_samples += frame["count"] if frame["started"] else 0
        data = chunk.get_audio_bytes()
        if chunk.audio_format.encoding == AudioEncoding.ULAW:
            data = audioop.ulaw2lin(data, 2)
        elif not chunk.audio_format.is_pcm16:
            raise ValueError("Unsupported SAASR transport encoding")
        if "sent" not in self.writers:
            self.writers["sent"] = WaveTrack(
                self.directory / "sent_to_agent.wav",
                sample_rate=chunk.audio_format.sample_rate,
            )
        self.writers["sent"].write(np.frombuffer(data, dtype="<i2") / 32767)

    def close(self):
        """Finish this trial's recordings without stopping workers used by other calls."""
        if self.closed:
            return
        self.cancelled.set()
        self.closed = True
        if hasattr(self.pool, "release_session"):
            self.pool.release_session(self)
        for writer in self.writers.values():
            writer.close()
        self.pending_ticks.clear()
        if hasattr(self, "tick_log"):
            self.tick_log.close()
        for index, worker in enumerate(getattr(self.pool, "workers", [])):
            log = worker.directory / "worker.log"
            if log.is_file():
                shutil.copyfile(log, self.directory / f"shared_worker_{index}.log")
        (self.directory / "render.json").write_text(
            json.dumps(self.record(), indent=2) + "\n"
        )


SAASR_AUDIO_FORMAT = AudioFormat(
    encoding=AudioEncoding.PCM_S16LE, sample_rate=PCM_SAMPLE_RATE
)
