from rotator_library.credential_manager import CredentialManager
from rotator_library.provider_factory import get_provider_auth_class
from rotator_library.providers import PROVIDER_PLUGINS
from rotator_library.providers.openai_codex_auth_base import OpenAICodexAuthBase


def test_credential_discovery_recognizes_openai_codex_env_vars(tmp_path):
    env_vars = {
        "OPENAI_CODEX_1_ACCESS_TOKEN": "access-1",
        "OPENAI_CODEX_1_REFRESH_TOKEN": "refresh-1",
    }

    manager = CredentialManager(env_vars=env_vars, oauth_dir=tmp_path / "oauth_creds")
    discovered = manager.discover_and_prepare()

    assert "openai_codex" in discovered
    assert discovered["openai_codex"] == ["env://openai_codex/1"]


def test_provider_factory_returns_openai_codex_auth_base():
    auth_class = get_provider_auth_class("openai_codex")
    assert auth_class is OpenAICodexAuthBase


def test_provider_auto_registration_includes_openai_codex():
    assert "openai_codex" in PROVIDER_PLUGINS
