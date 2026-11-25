"""Qwen's passive implementation of the shared async evidence port."""

from tau2.adapters.qwen_audio_agent.events import QwenEventAdapter


class QwenAsyncEvidenceSource:
    """Read Qwen queues; never perform task operations on the Agent's behalf."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.converter = QwenEventAdapter()

    def enable(self):
        """Enable native observation before the provider connects."""
        self.adapter.provider.persistent_tasks_enabled = True

    def get_capabilities(self):
        """Declare the pinned observer contract before model startup."""
        return {
            "version": "async-capabilities-v1",
            "task_identity": True,
            "states": ["queued", "running", "completed", "failed", "cancelled"],
            "operations": ["query", "result", "modify", "cancel"],
            "generated_reply": True,
            "multi_task": True,
            "update_confirmation": True,
            "cancel_confirmation": True,
            "adapter_version": "qwen-async-adapter-v1",
        }

    def drain_events(self):
        """Normalize received payloads exactly once on the consumer thread."""
        return [
            event
            for payload in self.adapter.provider.drain_backend_events()
            for event in self.converter.normalize_event(payload)
        ]

    def get_status(self):
        """Expose readiness and frontend playback boundaries without private access upstream."""
        provider = self.adapter.provider
        native = provider.backend_capabilities or {}
        expected = {"query", "result", "modify", "cancel"}
        gap = provider.backend_evidence_gap or bool(provider.failure)
        return {
            "ready": bool(native) and expected <= set(native.get("operations", [])),
            "evidence_gap": gap,
            "request_boundary": self.adapter.backend_request_boundary,
            "capabilities": self.get_capabilities() if native else None,
            "final_drain_complete": not provider.is_connected
            and provider.backend_events.empty(),
        }
