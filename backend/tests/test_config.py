"""配置只使用临时文件和假密钥，不能读取或修改开发者的真实密钥。"""

from app import config
from app.services.llm_service import get_model_settings


def clear_model_environment(monkeypatch):
    for name in ("PARK_AI_MODE", "DASHSCOPE_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "QWEN_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_env_file_loads_from_fixed_location_with_bom_and_literal_secret(tmp_path, monkeypatch):
    clear_model_environment(monkeypatch)
    backend = tmp_path / "backend"
    backend.mkdir()
    path = backend / ".env"
    path.write_text('PARK_AI_MODE=qwen\nDASHSCOPE_API_KEY="demo-${NOT_EXPANDED}#literal"\n', encoding="utf-8-sig")
    monkeypatch.setattr(config, "BACKEND_ENV_FILE", path)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    assert config.load_environment() is True
    settings = get_model_settings()
    assert settings.mode == "qwen"
    assert settings.api_key == "demo-${NOT_EXPANDED}#literal"
    assert settings.problem is None
    assert not (unrelated / ".env").exists()


def test_terminal_environment_takes_precedence_over_file(tmp_path, monkeypatch):
    clear_model_environment(monkeypatch)
    path = tmp_path / ".env"
    path.write_text("PARK_AI_MODE=qwen\nDASHSCOPE_API_KEY=demo-file-key\nQWEN_MODEL=qwen-plus\n", encoding="utf-8")
    monkeypatch.setattr(config, "BACKEND_ENV_FILE", path)
    monkeypatch.setenv("PARK_AI_MODE", "rule")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "demo-terminal-key")

    config.load_environment()
    settings = get_model_settings()
    assert settings.mode == "rule"
    assert settings.api_key == "demo-terminal-key"
    assert settings.model == "qwen-plus"
    assert settings.problem == "disabled"


def test_absent_env_file_keeps_offline_defaults_without_creating_files(tmp_path, monkeypatch):
    clear_model_environment(monkeypatch)
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "BACKEND_ENV_FILE", path)
    assert config.load_environment() is False
    assert get_model_settings().problem == "disabled"
    assert list(tmp_path.iterdir()) == []
