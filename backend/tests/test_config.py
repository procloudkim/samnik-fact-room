from samnik_backend.config import CheckpointerMode, RuntimeMode, Settings


def test_settings_accept_explicit_field_names_for_dependency_injection() -> None:
    settings = Settings(
        _env_file=None,
        runtime_mode=RuntimeMode.EXTERNAL,
        postgres_dsn="postgresql://local-test.invalid/samnik",
        elice_api_key="synthetic-test-key",
        embedding_model="intfloat/multilingual-e5-small",
    )

    assert settings.runtime_mode == RuntimeMode.EXTERNAL
    assert settings.postgres_dsn == "postgresql://local-test.invalid/samnik"
    assert settings.elice_api_key == "synthetic-test-key"
    assert settings.elice_base_url == "https://mlapi.run"
    assert settings.elice_model == "openai/gpt-4o-mini"
    assert settings.embedding_model == "intfloat/multilingual-e5-small"
    assert settings.checkpointer_mode == CheckpointerMode.NONE
