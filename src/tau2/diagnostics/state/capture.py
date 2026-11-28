"""Incremental dialogue evidence from transported chunks and confirmed ASR."""

from copy import deepcopy
from dataclasses import dataclass, field

from tau2.data_model.transcript import LiveTranscriptTick
from tau2.utils.transcript.audio_text import (
    extract_delivered_text,
    merge_audio_script_gold,
)

CAPTURE_VERSION = "tau-state-dialogue-capture/2"
_CONTROL = ("###STOP###", "###TRANSFER###", "###OUT-OF-SCOPE###")


@dataclass
class _Utterance:
    chunks: list = field(default_factory=list)
    identifiers: set = field(default_factory=set)
    start: float = 0.0
    end: float = 0.0
    first_tick: int = 0


class DialogueCapture:
    """Publish complete delivered utterances without using future script text."""

    def __init__(self, emit):
        self.emit = emit
        self.pending = {}
        self.live_fragments = set()
        self.live_count = 0

    def observe(self, speaker, chunk, tick_id, started, ended):
        """Append one consumed user chunk or one produced Agent media chunk."""
        if chunk is None:
            return
        raw = chunk.raw_data or {}
        live = raw.get("live_transcript") if speaker == "agent" else None
        if live is not None:
            self.finish_speaker(speaker, incomplete=True)
            self.observe_live(LiveTranscriptTick.model_validate(live), ended)
            return
        action = chunk.turn_taking_action
        if action is not None and action.action == "stop_talking":
            self.finish_speaker(speaker, incomplete=True)
            return
        if any(marker in (chunk.content or "") for marker in _CONTROL):
            self.finish_speaker(speaker, incomplete=True)
            return
        if chunk.is_tool_call():
            return
        identifiers = set(chunk.utterance_ids or [])
        pending = self.pending.get(speaker)
        if (
            pending is not None
            and identifiers
            and pending.identifiers
            and not identifiers.intersection(pending.identifiers)
        ):
            self.finish_speaker(speaker, incomplete=True)
            pending = None
        # Received silent chunks can carry active gold IDs for a TTS prelude.
        # Retain those IDs too, so played silence isn't mistaken for a missing
        # part of an otherwise fully delivered sentence.
        if (chunk.contains_speech and chunk.content) or chunk.audio_script_gold:
            if pending is None:
                pending = self.pending[speaker] = _Utterance(
                    start=started, end=ended, first_tick=tick_id
                )
            pending.chunks.append(deepcopy(chunk))
            pending.identifiers.update(identifiers)
            pending.end = ended
        # Some renderers end an utterance with an empty final chunk. A normal
        # inter-tick silence has no matching IDs and must not close a pause.
        if (
            pending is not None
            and chunk.is_final_chunk
            and (
                bool(chunk.content)
                or bool(identifiers.intersection(pending.identifiers))
            )
        ):
            self.finish_speaker(speaker)

    def finish_speaker(self, speaker, *, incomplete=False):
        """Commit only the received prefix when interrupted or the call ends."""
        pending = self.pending.pop(speaker, None)
        if pending is None:
            return
        gold = merge_audio_script_gold([c.audio_script_gold for c in pending.chunks])
        if gold:
            text, missing = extract_delivered_text(gold)
            incomplete |= missing
        else:
            text = "".join(c.content or "" for c in pending.chunks)
        incomplete |= any(
            (c.raw_data or {}).get("was_truncated") for c in pending.chunks
        )
        if not text.strip():
            return
        user = speaker == "user"
        self.emit(
            "user_turn" if user else "agent_turn",
            "user" if user else "frontend",
            text,
            ["frontend"],
            event_id=f"dialogue:{speaker}:{pending.first_tick}",
            timestamp=pending.end,
            metadata={
                "capture_version": CAPTURE_VERSION,
                "incomplete": bool(incomplete),
                "interval": [pending.start, pending.end],
                "utterance_ids": sorted(pending.identifiers),
            },
        )

    def observe_live(self, live, released):
        """Publish confirmed Agent fragments once, including silence-time releases."""
        for fragment in live.fragments:
            if fragment.speaker != "agent" or not fragment.text.strip():
                continue
            identity = (
                (live.session_id, fragment.event_id)
                if fragment.event_id is not None
                else None
            )
            if identity is not None and identity in self.live_fragments:
                continue
            self.live_count += 1
            self.emit(
                "agent_turn",
                "frontend",
                fragment.text,
                ["frontend"],
                event_id=f"live:{live.session_id}:{fragment.event_id or self.live_count}",
                timestamp=released,
                metadata={
                    "capture_version": CAPTURE_VERSION,
                    "incomplete": False,
                    "asr_fragment_id": fragment.event_id,
                    "asr_start_ms": fragment.start_ms,
                    "asr_end_ms": fragment.end_ms,
                    "alignment": live.alignment,
                },
            )
            if identity is not None:
                self.live_fragments.add(identity)

    def finish(self, final_live=None, *, released):
        """Flush partial prefixes and ASR that became final during shutdown."""
        for speaker in list(self.pending):
            self.finish_speaker(speaker, incomplete=True)
        if final_live is not None:
            self.observe_live(final_live, released)
