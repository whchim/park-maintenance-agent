"""独立内存演示入口；只在本机运行，不连接或迁移现有业务数据库。"""

import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def create_demo_app(repair_snapshot: dict | None = None):
    from fastapi import FastAPI
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base, get_db
    from app.main import app as business_app
    from app.models.repair_personnel import RepairPersonnel
    from app.services.attachment_service import ImageStore

    @asynccontextmanager
    async def lifespan(demo):
        # 图片与内存库同生命周期，仅清理本演示实例自行创建的临时目录。
        image_directory = TemporaryDirectory(prefix="park-demo-images-")
        demo.state.image_store = ImageStore(Path(image_directory.name))
        # 引擎只在演示实例启动时创建，固定内存 URL，不能由环境变量指向现有库。
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        factory = sessionmaker(bind=engine, autoflush=False)
        session_lock = Lock()
        try:
            Base.metadata.create_all(engine)
            with factory() as db:
                if repair_snapshot is not None:
                    from scripts.demo_repair_snapshot import restore_snapshot
                    restore_snapshot(db, repair_snapshot)
                for number, name, skills, area in [
                    ("ENG-DEMO-001", "空调维修模拟员", ["空调", "设备"], "A栋"),
                    ("ENG-DEMO-002", "水电维修模拟员", ["漏水", "照明", "水电"], "A栋"),
                    ("ENG-DEMO-003", "机电维修模拟员", ["设备", "门禁"], "B栋"),
                ]:
                    if repair_snapshot is not None:
                        continue
                    db.add(RepairPersonnel(personnel_no=number, display_name=name, department="工程部",
                                           role="维修人员", skills=json.dumps(skills, ensure_ascii=False), responsible_area=area))
                db.commit()

            def demo_db():
                # StaticPool共享同一SQLite连接。并行HTTP读取也必须串行使用，
                # 否则一个Session关闭时的回滚可能干扰另一个Session的事务。
                # 依赖进入/退出可能处于不同工作线程，因此使用非线程所有权的Lock。
                with session_lock:
                    with factory() as db:
                        yield db

            demo.dependency_overrides[get_db] = demo_db
            yield
        finally:
            demo.dependency_overrides.pop(get_db, None)
            engine.dispose()
            image_directory.cleanup()

    demo = FastAPI(title="园区智维 · 隔离演示", lifespan=lifespan)
    demo.include_router(business_app.router)
    demo.state.environment = {"name": "isolated_demo", "synthetic_data": True, "volatile": True}
    return demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="显式启用真实千问；只输入模拟资料，调用会消耗账号额度")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--restore-repair-snapshot", type=Path, help="仅恢复旧版报修模拟快照到本次新内存库；原进程保留")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口需在1024至65535之间")
    # 仅改变本次进程，既不写 .env，也不让本机恰好存在密钥就触发调用。
    os.environ["PARK_AI_MODE"] = "qwen" if args.online else "rule"
    from app.services.llm_service import runtime_status
    if args.online and not runtime_status()["ready"]:
        parser.error("千问配置未就绪，请在本机检查 backend/.env；不要将密钥发到聊天中")
    import uvicorn
    snapshot = json.loads(args.restore_repair_snapshot.read_text(encoding="utf-8")) if args.restore_repair_snapshot else None
    uvicorn.run(create_demo_app(snapshot), host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
