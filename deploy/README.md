# 求职演示部署

目标链路：面试官浏览器 → 域名 / HTTPS → 服务器上的 Caddy → 静态前端或 FastAPI → 独立 SQLite / 图片卷。所有程序在服务器运行，开发者电脑关闭不影响网站；不需要内网穿透。

这里只提供部署准备。默认绑定 `127.0.0.1:8080`，不会自动发布公网。访问者共享演示数据与模拟角色，请勿输入真实客户、联系方式或人员资料。演示密码不是企业账号权限体系。

## 1. 准备

服务器安装 Docker Engine 和 Compose 插件，把项目源码上传或拉取到服务器。在项目根目录执行后续命令。代码目录不应包含开发机 `.env`、数据库、图片、`deliverables` 或个人交付资料；`.dockerignore` 同时排除这些文件，镜像只复制明确需要的程序。

第一次设置演示访问密码，运行官方 Caddy 的交互命令：

```sh
docker run --rm -it caddy:2.10-alpine caddy hash-password
```

交互输入较长的随机密码，保存输出的 bcrypt 哈希。明文密码由你保管，按需单独提供给面试官；不要把真实密码、API Key 发到聊天、提交到 Git 或写进命令参数。

复制 `deploy/.env.example` 为 `deploy/.env.demo`，填写：

```dotenv
DEMO_USER=demo
DEMO_PASSWORD_HASH='$2a$...这里替换成完整的bcrypt哈希...'
PARK_AI_MODE=rule
```

哈希必须保留完整内容并用单引号包裹，避免 `$` 被环境文件插值。这里没有默认密码，空哈希会阻止 Compose 启动。部署文件已被 `.gitignore` 忽略。

如果服务器有多个本机用户，限制该配置文件的读取权限，例如 Linux 下 `chmod 600 deploy/.env.demo`。不要运行会把完整环境变量打印到聊天或日志中的命令。

## 2. 先验证本机部署

```sh
docker compose --env-file deploy/.env.demo config --quiet
docker compose --env-file deploy/.env.demo up -d --build
docker compose --env-file deploy/.env.demo ps
docker compose --env-file deploy/.env.demo logs --tail 100 backend gateway
```

访问 `http://127.0.0.1:8080`，浏览器会弹出演示账号和密码输入框。页面、API、图片与网关健康检查全部需要口令，后端没有映射到宿主机的公开端口。首次启动创建独立空库及三名模拟维修人员，之后重启保留工单和图片。

服务器本机验证时可使用 SSH 本地转发：

```sh
ssh -L 8080:127.0.0.1:8080 你的SSH用户@服务器IP
```

随后在自己电脑访问上述本机地址。这里的 SSH 转发只用于上线前验证；公开演示走下一节的域名入口。

## 3. 用真实域名公开演示

域名解析到服务器公网 IP，并确保适用的域名、主机及站点手续已处理。服务器防火墙和云安全组开放 TCP 80、443；UDP 443 可选。后端 8000 和本机验证端口 8080 不开放公网。

在 `deploy/.env.demo` 添加你自己拥有的真实域名，仅写域名，不写 `http://`、路径或端口：

```dotenv
DEMO_DOMAIN=你的真实域名
```

```sh
docker compose --env-file deploy/.env.demo -f compose.yaml -f deploy/compose.public.yaml config --quiet
docker compose --env-file deploy/.env.demo -f compose.yaml -f deploy/compose.public.yaml up -d --build
```

Caddy 负责域名入口的 HTTPS 和续期。只有在域名解析、端口可达、证书签发成功后，才算完成公网部署；浏览器访问 `https://你的真实域名` 验证。不要把 HTTP 本机配置改成 `0.0.0.0:8080` 来替代 HTTPS。

## 4. 可选：启用真实模型

默认 `PARK_AI_MODE=rule`，没有远端模型请求。需要真实千问时，在私有部署配置中填写 Key、地域匹配的官方入口及账号可用模型，再设 `PARK_AI_MODE=qwen` 并重新执行对应的 `up -d` 命令。不要将 Key 放进前端环境变量。

默认限制：

| 项目 | 默认值 | 说明 |
| --- | --- | --- |
| 日调用额度 | 100 次 | 全站共享，UTC日统计，失败请求也计数；一次工程建议最多请求3次，不等于100张工单 |
| 分钟调用额度 | 10 次 | UTC分钟固定窗口，全站共享，重启不会清空 |
| 同时模型请求 | 2 个 | 单后端进程；达到上限直接规则回退 |
| 单次模型超时 | 15 秒 | 已有网络边界超时 |
| 单次模型最大输出 | 1600 Token | 沿用已有结构化及工具调用限制 |
| 网关请求体 | 11 MB | 附件业务入口仍限制每张图片10 MiB，校验图片格式与像素 |

可以修改 `PARK_MODEL_DAILY_LIMIT` 和 `PARK_MODEL_PER_MINUTE_LIMIT` 为正整数。账本位于独立数据卷内的 `model-budget.sqlite3`；账本不可用或额度耗尽时不发送模型请求，前端显示对应规则回退原因。请求次数不是人民币预算，仍需在供应商控制台设置适用的账户额度和告警。

当前只支持单实例、小规模演示。不要添加 `--workers` 或横向扩容来宣称高并发；现有工单编号逻辑仍有并发边界。飞书桥接目前要求原本机隔离演示入口，本部署不会自动接通飞书云端协作。

## 5. 数据保存、备份与更新

`demo-data` 卷保存 `park.db`、`uploads/` 和模型请求账本；`caddy-data` / `caddy-config` 保存证书与代理运行配置。普通 `docker compose down` 不删除命名卷。**不要使用 `down -v` 或清理数据卷来更新应用。**

先在项目目录创建 `backups` 目录。为了让数据库和附件形成同一份一致快照，备份前停止业务容器；不需要停止网关。以下命令以仅本机配置为例，公网部署时给 Compose 命令增加同样的两份 `-f` 参数：

```sh
docker compose --env-file deploy/.env.demo stop backend
docker compose --env-file deploy/.env.demo run --rm --no-deps -v "./backups:/backup" backend python -c "import datetime,tarfile; p='/backup/demo-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.tar.gz'; t=tarfile.open(p,'x:gz'); t.add('/data',arcname='data'); t.close(); print(p)"
docker compose --env-file deploy/.env.demo start backend
```

Linux 下备份目录需允许容器 UID 10001 写入，例如由服务器管理员为该专用目录设置对应所有权。备份可能包含访问者输入，仅本地保存并限制权限，不提交仓库。

更新前备份、保留旧镜像及版本记录，再拉取代码并执行对应 `up -d --build`。现有数据库缺表或缺列时，入口会拒绝启动并指出差异，**不会自动修复或清空旧数据**；需先安排迁移，不能直接复制开发机数据库覆盖演示库。回滚时代码、数据库结构和附件需要兼容，不能保证任意旧版本可直接读取新库。

## 6. 验收清单

- 不输入密码或密码错误，页面、API 和图片都返回401。
- 正确密码能进入前端；运行状态显示“求职演示”，不将规则结果标为模型成功。
- 新建 → 分派 → 接单 → 开始 → 提交维修 → 退回 → 再次提交 → 验收通过；历史完整。
- 上传模拟图片后重启容器，工单、验收意见和图片依旧可读。
- 模型额度耗尽/网络失败后仍能进行普通业务操作，页面明确说明回退。
- 公网确认 HTTPS 无证书错误，外部不能直接访问后端8000端口。

相关说明：[部署架构](../docs/architecture/ADR-012-演示部署与访问保护.md)、[验证记录](../docs/verification/20261109-演示部署准备.md)。
