# 园区运营多 Agent 协同平台

面向园区招商、企服与工程协作的 AI 应用 Demo：以飞书机器人作为员工入口，Vue Web 工作台承载运营与验收，FastAPI 业务后端管理流程、状态和数据，LangGraph 与通义千问辅助信息补全、维修反馈整理及工程委派建议。

当前重点是 **企服报修 → 人工确认 → 工程委派 → 维修反馈 → 企服验收**，并扩展招商客户建档、回访与签约客户交接。目录、人员和验收案例均使用模拟资料。

## 已实现能力

| 业务域 | 当前能力 |
| --- | --- |
| 企服 | 自然语言报修完整性检查、缺项追问、人工确认建单、进度查询、Web 验收及退回重修 |
| 工程 | 查询模拟人员目录与未完成分派数，生成委派建议；人工派工、接单、开始维修、图文反馈及提交确认 |
| 招商 | 意向信息结构化草稿、客户建档、A/B/C 回访任务、跟进记录及签约客户交接的提交、退回、更正和接收 |
| 双端协同 | 飞书单聊长连接与交互卡片、Web 状态轮询、旧卡片失效、图片上传和业务绑定 |
| 自动提醒 | 工程新单、回访到期、接单/处理超期，以及按成功通知回执计时的特急 5 分钟提醒 |

AI 提供理解和建议，业务 Service 执行权限、事务与状态校验；建单、委派、维修提交和验收保留人工确认。模型失败时显示规则回退，不把离线结果当作真实模型结果。

## 架构与技术栈

```text
飞书机器人（官方长连接 SDK）       Vue 3 / TypeScript / Vite
             │                           │
             └──────── FastAPI API ───────┘
                            │
          LangGraph / Qwen ──┤── 业务 Service / 状态机
          信息整理、只读工具  │   人工确认、事务、事件追溯
                            │
                    SQLAlchemy / SQLite
```

工程 Agent 的工具限定为只读人员目录与未完成任务查询，单次建议最多 3 次模型请求、4 次工具调用。最终建议需匹配实际目录快照，Agent 不直接操作业务数据库。

部门路由采用确定性业务规则；企服与工程使用 LangGraph，招商使用结构化模型任务。目前尚未形成三个部门完全自主协作的 Agent 网络，也未启用 LangGraph 原生检查点与中断恢复。

## 本地运行

环境：Python 3.11+，Node.js 20.19+ 或 22.12+。以下命令使用 Windows PowerShell，在项目根目录执行。

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
& .\.venv\Scripts\python.exe backend\scripts\run_demo.py --port 8003
```

该入口使用独立内存数据库、临时图片目录及三名模拟工程人员，默认离线规则模式，不迁移现有业务数据库。**停止进程后，本次演示数据会清空。**

另开终端启动前端：

```powershell
cd frontend
npm ci
$env:API_PROXY_TARGET='http://127.0.0.1:8003'
npm run dev -- --port 5174 --strictPort
```

访问 <http://127.0.0.1:5174/>，接口文档位于 <http://127.0.0.1:8003/docs>。如端口已占用，请为新实例选择其他端口并同步修改前端代理目标。

### 可选：真实千问与飞书

将 `backend/.env.example` 复制为本机 `backend/.env`，填写百炼 API Key 和匹配地域的模型配置；不要提交真实凭证。配置完成后，以 `run_demo.py --online --port 8003` 显式启用真实千问，调用会消耗账号额度。

飞书通过独立桥接进程连接同一个后端，需配置企业自建机器人、消息权限、卡片回调与显式模拟角色。步骤和启动参数见[飞书接入说明](docs/飞书自建应用接入说明.md)，模型配置见[千问接入与验收](docs/千问接入与验收.md)。

## 验证

安装测试依赖后，从项目根目录执行后端离线回归：

```powershell
& .\.venv\Scripts\python.exe -m pip install pytest httpx
Push-Location backend
$env:PYTHONDONTWRITEBYTECODE='1'
$taskTestTemp=Join-Path (Get-Location).Path ('.local\pytest-'+[guid]::NewGuid().ToString('N'))
& ..\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp $taskTestTemp
Pop-Location
```

Agent 固定模拟用例检查默认不调用真实模型、不写业务数据库：

```powershell
& .\.venv\Scripts\python.exe backend\scripts\verify_agents.py
```

前端在 `frontend` 目录执行 `npm run build`，包含 TypeScript 检查。各阶段自动化与真实客户端验证范围见[验收记录](docs/verification/README.md)。

## 范围与后续工作

- Web 与飞书共享最终业务数据；报修草稿尚未实现跨端共享，不能在另一端直接确认同一草稿。
- 企服最终维修验收在 Web 办理；飞书群聊、完整招商对话与正式组织身份体系待完善。
- 特急 5 分钟提醒提供人工电话联系提示，不自动拨号；部分超期提醒目前只有离线时间模拟验证。
- 费用催缴、公共资产预约、退租、完整工程改造与巡检闭环不在当前 Demo 完成范围。
- 当前为本机模拟演示应用，正式部署前需完善生产鉴权、身份映射、持久化部署与可靠投递机制。

## 项目资料

- [项目上下文与开发约束](docs/CODEX_CONTEXT.md)
- [PRD](docs/园区运营多Agent协同平台_PRD.md)
- [部门负责人确认矩阵](docs/部门负责人确认矩阵.md)
- [架构选型报告](docs/architecture/README.md)
- [验收记录](docs/verification/README.md)

源码包含迁移脚本供审查，但隔离演示启动不执行这些脚本。真实密钥、数据库、上传图片、本机运行记录与个人交付资料不随仓库提交。
