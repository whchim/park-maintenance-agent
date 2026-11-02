"""固定位置加载本机配置；配置文件不依赖启动命令所在目录。"""

from pathlib import Path

from dotenv import load_dotenv


BACKEND_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_environment() -> bool:
    # 已存在的终端/部署环境变量优先；文件中的密钥不做 ${...} 插值。
    # utf-8-sig 同时兼容普通 UTF-8 和 Windows 编辑器写入的 UTF-8 BOM。
    return load_dotenv(
        dotenv_path=BACKEND_ENV_FILE,
        override=False,
        encoding="utf-8-sig",
        interpolate=False,
    )
