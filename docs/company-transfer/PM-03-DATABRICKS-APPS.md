# 方案三：公司 Databricks Apps 托管 Business AI Hub

日期：2026-10-22。适用原因：公司电脑无法安装 Python。此方案替代“公司电脑本机运行”；目前仅完成源码与官方能力核对，未连接公司环境复核，也未创建、修改或部署资源。

## 推荐方案

```text
公司电脑浏览器
    ↓ 公司登录及 App 使用权限
Databricks Apps：现有网页 + FastAPI + Task Manager 组件
    ├─ 公司已有 Genie One
    ├─ 已有 SQL Warehouse
    ├─ 一个已有轻量模型
    └─ 两张持久任务/运行记录表
```

Python、依赖安装和服务运行放在 Databricks Apps。公司电脑只需浏览器与代码文件上传，不必安装 Python、Node、Databricks CLI 或访问 GitHub。

Apps 官方支持从 Workspace 文件夹部署，前提是公司未强制 Git-only 部署。禁止把普通 Notebook 中运行 uvicorn 当作已完成的网页托管方案。

## 当前代码可复用与需适配项

可复用：React 页面、FastAPI API、自然语言分流和草稿模型、Genie One MCP 适配器、SQL 生成/测试/复用、任务状态、报告和邮件组件。已有基础 app.yaml，但不代表当前 ZIP 已能直接完成公司 Apps 部署。

代码侧需完成：

1. 将 Hub、Task Manager 代码/模板及合并后的运行依赖放入同一个 App 部署根目录，修正 TASK_MANAGER 路径。只上传 Hub 子目录会缺少其外部依赖。
2. 增加专用 Databricks Apps 身份适配。当前后端是内网 Web OAuth/本地 CLI 模式，不能直接使用 Apps 环境变量就假定鉴权正常。
3. 沿用共享试点要求时，显式采用 App 自带服务主体进行业务调用，并绑定任务到该运行身份，不静默假装是原创建者。保持正式用户权限模式的代码独立；不关闭 Databricks 平台的登录。
4. 元数据 SDK 身份接入 App 自带身份，避免要求不存在的本机 SDK profile。
5. 任务和运行记录使用持久存储。推荐现有源码对应的两张 Delta 表；不要以 App 本地 SQLite 保存需要跨重启保留的任务。短期纯界面演示可以临时存本地，但必须明确重启可能丢失，不能算完整任务系统。
6. 把手动试运行、报告预览和明确发信从个人本地启动器拆入 App 入口，并持久化所需状态。后台自动调度、邮件和 CSV 仍需分别接入/验收，不因 App Running 就认为完成。

## 给 PM 的最小确认清单

| 项目 | 请求 |
|---|---|
| Apps 运行条件 | 公司 Workspace 允许创建/运行一个试点 App，有可用配额/使用政策，并批准试点运行费用；若现有 App Space 有合适权限则复用 |
| 开发与访问 | 给实施者该试点 App 的 CAN MANAGE；试点同事 CAN USE。不是申请 Workspace 全局管理员。App Space 如适用，其创建权限由管理员按现有政策核对 |
| 源码与依赖 | 允许从 Workspace 文件夹部署；确认是否有 Git-only 政策。公司批准的源码目录可供 App 读取，Apps 构建能访问批准的依赖源。浏览器可以访问平台生成的 App 域名 |
| App 自带身份 | App 创建时平台自动配置服务主体，不用手工另建一套账号或把个人 Token 上传。管理员仍须授予/确认下列资源访问，当前个人账号的权限不会自动转移给它 |
| 业务资源权限 | 对 App 身份确认：Genie One 服务及父级 USE/EXECUTE、实际需要的 Agent 使用及业务数据读取；Warehouse 540a674067e832d2 CAN_USE 及所需 SQL 使用资格；一个模型的调用权限 |
| 任务持久化 | 在获批且隔离合适的现有 Schema 创建 business_task_registry_v1、business_task_runs_v1。App 身份仅对这两表 SELECT/MODIFY 及父级 USE；不申请业务数据写入。Schema 名及建表由平台/ Terraform 流程确认 |

公司之前的只读检查发现 hello-world、rag-qa-app 两个 App。这只能说明已有相关资源，不能证明当前用户可以创建新 App、修改现有 App，或者它们适合复用。不得覆盖这两个 App。

## 登录与成本边界

Databricks Apps 不能匿名公开访问或绕过平台 SSO。可以先让应用内的试点人员共用 App 的业务数据权限，但访问页面本身仍需要公司认可的身份和 CAN USE。

Apps 创建时自动生成自身身份并在运行时提供 SDK 认证配置；不必为这条共享 App 身份方案另行注册内网网页登录 OAuth 应用。业务调用是否获准仍由资源权限决定，不能复制个人用户权限的结论。

Apps 按运行的计算容量/时间计费；即使没有人提问，只要 App 运行也有托管费用。SQL、Genie、模型用量另计。此前“个人电脑 + 免费隧道、空闲新增云费用约 0”不适用于本方案。实际金额需公司管理员按账户价确认；不在本轮创建 App 或启动计算估价。

## 公司 Genie Code 第一轮只读检查提示词

公司电脑不能安装 Python。请只读评估现有 Business AI Hub 改为 Databricks Apps 托管的可行性，不创建或部署 App，不启动 Warehouse/计算，不运行 SQL/模型，不修改权限。

请核对：
1. 本 Workspace 的 Databricks Apps 是否可用，当前身份能否创建试点 App，或需要谁授予对应权限；已有 App Space 是否可复用。
2. 是否允许从 Workspace 文件夹部署，是否强制 Git-only；GitHub 不可达时公司允许什么源码方式。
3. Apps 的配额、使用政策、依赖源网络和公司浏览器对 App 域名的访问条件；可读到则列出，否则标注未验证。
4. 现有 Warehouse dydf_dev_sqlwh（540a674067e832d2）、Genie One MCP system.ai.genie_one_mcp、一个已有轻量模型，未来 App 自带服务主体需要哪些缺失权限。App 还没创建时不能假设其权限已经存在。
5. 是否可由平台在受控 Schema 准备 business_task_registry_v1 和 business_task_runs_v1 两表；先确定位置和负责人，不自行建表。
6. 已有 hello-world、rag-qa-app 不要覆盖，也不修改其资源或身份。

源码目前在公司电脑 Documents。若你不能读取，请明确告知需要上传到哪个你能读取的 Workspace 位置，以及本 Workspace 实际支持的上传方式；不要假定已读取 Documents 或普通 ZIP 能自动导入解压。拿到代码后先检查打包路径、Apps 身份适配与持久存储方案，再给待审核修改方案。

最终只返回“可用/未验证/缺少”的短表和最少管理员动作。不得要求迁移 Genie 知识、重建 Agent、创建 Supervisor，或默认给全局管理员权限。

## 官方依据

- Apps 托管环境：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/system-env
- Workspace 目录部署及 Git-only 策略：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/deploy
- Apps 自动身份与用户身份：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/auth
- Apps CAN USE/CAN MANAGE 及禁止匿名访问：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/permissions
- 状态与持久化：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/key-concepts
- 按运行时间计费：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/
