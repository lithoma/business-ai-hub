# Business AI Hub：内网单账号试点最小申请清单

日期：2026-10-21。本清单替代上一次针对“正式多人 OAuth + 云端元数据”的首批申请方案。

## 方案结论

系统运行在公司内网电脑，任务与运行记录先保存在该电脑的 SQLite。使用一个公司批准的现有 Databricks 用户，通过公司允许的 CLI/SDK OAuth 登录，统一调用已有 Genie One、SQL Warehouse 和一个轻量模型。

不必为首轮试点新建自定义 Web OAuth 应用、服务主体、Unity Catalog 任务表、专用 Schema、Key Vault、Databricks App 或 Job。无需迁移 Genie 知识、创建 Agent 或申请管理员权限。

该模式下访客共用运行账号的业务权限，暂不区分访客身份。只对有权使用该账号数据范围的内网试点人员开放，按 PM/IT 批准范围运行。个人开发 Workspace 的公开隧道不会连接公司环境。

## 请 PM 转交的最小申请

请批准 Business AI Hub 在公司内网电脑进行单账号试点。优先使用一个已有、获准的公司 Databricks 账号，不新建服务身份；候选为当前检查账号 `yulong.x.mao@haleon.com`，具体由项目负责人确认。

请资源负责人只检查并补齐该账号缺少的以下三类调用权限，已有继承授权不重复授予：

| 资源 | 最小调用权限 | 公司现状 |
|---|---|---|
| Genie One 与现有业务资产 | `system` 的 USE CATALOG、`system.ai` 的 USE SCHEMA、`system.ai.genie_one_mcp` 的 EXECUTE；实际使用的现有销售/竞争 Agent 的 CAN RUN；查询所需业务 Catalog/Schema 的 USE 及表/视图/Metric Views 的 SELECT，保留既有行/列规则 | 当前用户 MCP 及父级权限已由公司报告确认；Agent 调用、数据与实际 API 连通仍需核对 |
| 现有 SQL Warehouse | `dydf_dev_sqlwh` / `540a674067e832d2` 的 CAN_USE，以及 Databricks SQL 所需 entitlement。只运行已有 SQL Warehouse，不申请新建/管理计算资源权限 | Warehouse 已存在且 Stopped；CAN_USE 未确认 |
| 一个现有轻量模型 | 仅选一个兼容结构化输出的现有资源。若使用 Serving Endpoint（候选 `databricks-gpt-oss-20b`）：CAN_QUERY。若使用 Unity Gateway Model Service：该服务 EXECUTE 和父级 USE。两条接入方式选一种，不重复申请 | 已发现 READY 候选，具体调用 ACL 和结构化输出未验证 |

同时只需确认以下电脑使用条件：

- 允许安装/运行 Python 3.11+、项目 Python 依赖和 Databricks CLI（使用公司批准的软件及依赖源）。
- 允许该账号使用现有 CLI/SDK OAuth 登录；若公司禁用了内置 CLI OAuth 客户端，由身份管理员确认允许的现有登录方式。不能假定一定要新注册 Web OAuth 应用。
- 公司电脑可访问 `https://adb-7405618086077365.5.azuredatabricks.net` 的 OAuth、Genie One MCP、SQL REST 和模型 API，以及公司登录/SSO 所需地址。
- 电脑能在公司批准的位置写入任务数据库、报告与日志。只在本机使用可绑定 localhost；若其他内网电脑访问，再由 IT 批准对应内网端口和访问范围，无需公网域名或公开隧道。

请返回：批准的运行账号、Warehouse CAN_USE 确认、可调用的准确模型 ID、Genie One/Agent/数据访问确认，以及电脑软件/网络是否满足。不要通过聊天、Markdown 或 ZIP 传递密码、Token 或其他凭据。

## 邮件与自动运行分别处理

邮件不是“网页能够打开并完成分析/创建任务”的前置条件。如果首轮也要求真正发邮件，只需额外批准一种公司已有发件通道和发件身份，明确允许的收件范围；无需同时申请 Gmail、SMTP 和 Graph。当前有 Gmail 适配器，其他通道需补代码，授权本身不会完成适配。

后台自动运行可以优先适配本机调度，无需因此创建 Databricks Job。仍需确认公司是否允许该运行身份在无人值守时使用、CLI/SDK 凭据刷新是否满足，以及电脑持续运行条件；当前调度/后台入口尚需代码适配，不能将“审批了账号”当作已实现自动化。

## 必须明确的代码状态

此方案是针对新要求精简的接入方案，**公司内网单账号 SQLite 启动方式尚未接入已传输的 ZIP**。原包 `config/company.yaml` 仍采用个人 Web OAuth + 云端元数据表，不能直接删掉配置项就启动。

个人电脑现有本地 SQLite 工作流可以复用，但启动器需要改为公司批准的账号、Workspace、Warehouse、模型和本地日期设置，并去除开发环境专用假设。当前共享试用启动器特意限制为个人开发 Workspace，也不能直接拿到公司使用。无需为绕过这段代码适配先申请更多云权限；代码侧可独立完成。

## 参考

- CLI/SDK 用户 OAuth：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/auth/oauth-u2m
- Genie One MCP：https://learn.microsoft.com/en-us/azure/databricks/agents/mcp/genie-mcp
- MCP 父级与服务权限：https://learn.microsoft.com/en-us/azure/databricks/ai-gateway/govern-mcp-service
- SQL 调用与数据权限：https://learn.microsoft.com/en-us/azure/databricks/dev-tools/sql-execution-tutorial
- Serving Endpoint 调用权限：https://learn.microsoft.com/en-us/azure/databricks/machine-learning/model-serving/manage-serving-endpoints
