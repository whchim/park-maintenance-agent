import pytest


@pytest.fixture(autouse=True)
def offline_model_by_default(monkeypatch):
    """常规测试不使用开发者本机密钥；模型测试显式启用并替换网络传输。"""
    monkeypatch.setenv("PARK_AI_MODE", "rule")
