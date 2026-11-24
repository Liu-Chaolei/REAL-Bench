"""Offline checks for Gaia2 caller profiles and main/hidden task selection."""

import pytest

from tau2.data_model.language import TaskSelection
from tau2.domains.gaia2.language import load_pack_bytes
from tau2.domains.gaia2.language_runtime import load_language, resolve_task
from tau2.registry import registry
from tau2.user.gaia2_guidelines import get_gaia2_user_guidelines


@pytest.mark.parametrize("mode", ["text", "voice"])
def test_gaia2_profiles_render_without_service_roles(mode):
    loaded = load_language(
        "en", "gaia2", mode=mode, selection=TaskSelection(task_split_name="search")
    )
    task = loaded.tasks[0]
    context = resolve_task(loaded, task, 300)
    prompt = get_gaia2_user_guidelines(context.persona, mode=mode, use_tools=False)
    assert "current released request" in prompt
    assert "application databases, reference answers" in prompt
    assert "supervisor" not in prompt.lower()
    assert context.persona_id in {"gaia2_male", "gaia2_female"}
    assert (context.persona.voice is not None) == (mode == "voice")
    assert context.artifact_pins == loaded.artifact_pins


def test_hidden_tasks_share_gaia2_runtime_and_caller_facts():
    loaded = load_language(
        "en",
        "gaia2",
        mode="text",
        selection=TaskSelection(task_set_name="gaia2_hidden"),
    )
    assert len(loaded.tasks) == 100
    context = resolve_task(loaded, loaded.tasks[0], 300)
    assert context.caller_gender == "female"
    assert context.persona.gender == context.caller_gender
    assert context.canonical_task_id.startswith("hidden_")


def test_only_gaia2_is_exposed_as_an_environment():
    assert registry.get_domains() == ["gaia2"]
    assert set(registry.get_task_sets()) == {"gaia2", "gaia2_hidden"}
    pack, _ = load_pack_bytes()
    assert set(pack.domains) == {"gaia2"}
    assert pack.default_binding is None


@pytest.mark.parametrize("language,domain", [("es", "gaia2"), ("en", "retail")])
def test_retired_language_and_domain_selection_fails_before_providers(language, domain):
    with pytest.raises(ValueError, match="only en/gaia2"):
        load_language(language, domain, mode="text")
