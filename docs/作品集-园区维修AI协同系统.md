# 园区智维｜从自然语言到可控业务执行

**AI 应用开发作品｜个人项目 · 基于企业测试题持续迭代**

[GitHub 源码](https://github.com/whchim/park-maintenance-agent) · [业务流程图](https://github.com/whchim/park-maintenance-agent/blob/main/docs/作品集-维修协同流程图.md) · [部署说明](https://github.com/whchim/park-maintenance-agent/blob/main/deploy/README.md) · [验证记录](https://github.com/whchim/park-maintenance-agent/blob/main/docs/verification/20261109-演示部署准备.md)

Python / FastAPI · Vue 3 / TypeScript · LangGraph · Qwen · SQLAlchemy / SQLite · 飞书 SDK

## 一条报修消息，如何变成可信的业务流程？

自然语言报修可能缺少地点或必要说明，模型建议也需要事实依据。本项目将信息提取、缺项追问与受限工具调用接入工单系统，通过人工确认、业务状态校验和历史记录管理，将 AI 输出转为可核对、可执行、可追溯的协作流程。

**典型流程：** 自然语言报修 → 补全信息并确认建单 → 查询人员与未结束分派数 → 人工派单 → 图文维修反馈 → 验收 / 退回返工。飞书承载本机消息和卡片协作，Web 承载运营与最终验收。

## 三个关键技术决策

- **用证据约束生成。** 报修字段经 Schema 与原文证据校验；信息不足时追问，模型超时或输出无效时明确展示规则回退，让使用者知道结果来自模型还是规则。
- **为 Agent 划定执行边界。** 使用 LangGraph 组织“模型决策 → 只读工具 → 继续判断”的循环，校验工具白名单、参数和候选人员；单次建议最多 **3 次模型请求、4 次工具调用**，最终派单由人确认。
- **将推理与业务事务分离。** 工程建议使用独立只读请求，避免在建单写事务中等待模型；Web 与飞书复用业务服务，由服务层校验状态并落库。维修凭证、多轮验收人、意见及返工记录持久化，跨请求续办使用应用事件。

## 如何证明它能运行？

- **自动化验证：** 290 项后端离线测试、6 项前端组件测试通过，覆盖工具约束、异常回退和业务状态等场景；前端类型检查及生产构建通过。
- **真实容器联调：** Docker / Caddy 的 6 组验证通过，包含密码访问、前后端联通、完整返工闭环，以及重启后工单、历史记录和图片保留。
- **部署与费用控制：** 统一口令入口、独立数据卷；默认离线，启用模型后限制日 / 分钟请求数及并发，配额跨重启保留，耗尽或故障时回退。

**已交付：** GitHub 源码、[数据库设计](https://github.com/whchim/park-maintenance-agent/blob/main/docs/数据库设计.md)、[架构决策](https://github.com/whchim/park-maintenance-agent/tree/main/docs/architecture)、自动化测试及可复现的容器验证脚本。

**状态更新：2026-11-09。** 使用 AI 辅助编码与模拟资料；源码已推送 GitHub，本机容器验收通过，公网待部署。正式组织权限、真实场景模型效果评测及飞书云端桥接为后续工作。
