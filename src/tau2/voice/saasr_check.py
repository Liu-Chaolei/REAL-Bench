"""Render a multi-turn caller audio check without any Agent or judge API calls."""

import argparse
import json
from pathlib import Path

from tau2.data_model.message import UserMessage
from tau2.data_model.saasr import SaasrConfig
from tau2.data_model.voice import SynthesisConfig, VoiceSettings


def main():
    """Exercise the same caller TTS, chunking, mixing and transport as voice runs."""
    from tau2.user.user_simulator_streaming import VoiceStreamingUserSimulator
    from tau2.voice.saasr import SaasrSession
    from tau2.voice.saasr_worker import close_pools

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--text", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    settings = SaasrConfig.from_file(args.config)
    identity = settings.preflight(args.domain, [args.task_id])
    user = None
    session = None
    try:
        session = SaasrSession(
            settings,
            settings.scene_for(args.domain, args.task_id),
            args.output,
            seed=args.seed,
            language="English",
            identity=identity,
        )
        user = VoiceStreamingUserSimulator(
            tools=[],
            instructions="Audio renderer check",
            llm="unused",
            voice_settings=VoiceSettings(
                transcription_config=None, synthesis_config=SynthesisConfig()
            ),
            chunk_size=1600,
            tick_duration_seconds=0.1,
            realtime_generation=True,
            use_llm_backchannel=False,
            saasr_session=session,
        )
        state = user.get_init_state()
        for text in args.text:
            message = user.synthesize_voice(
                UserMessage(role="user", content=text), state
            )
            for chunk in user._create_chunk_messages(message):
                session.record_sent(user._mix_saasr_chunk(chunk, state))
            # The environment keeps playing while the caller listens.
            for _ in range(10):
                session.record_sent(user._mix_saasr_chunk(None, state))
        user.stop(state=state)
        result = {
            "kind": "audio_only_check",
            "agent_accuracy_measured": False,
            "texts": args.text,
            "render": session.record(),
        }
        (args.output / "check.json").write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "duration_seconds": session.sent_samples / 16000,
                    "foregrounds": len(args.text),
                },
                indent=2,
            )
        )
    finally:
        if user is not None:
            user.stop()
        elif session is not None:
            session.close()
        close_pools()


if __name__ == "__main__":
    main()
