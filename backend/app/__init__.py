from app.config import load_environment


# 加载一次本机配置后再导入各业务模块；修改 .env 后需要重启后端。
load_environment()
