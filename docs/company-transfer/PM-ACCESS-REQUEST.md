# Business AI Hub：公司接入授权申请清单

> 适用正式多人 OAuth 与云端元数据部署。2026-10-21 新确定的首轮内网单账号试点，请改用 `PM-MINIMAL-INTRANET-ACCESS.md`；不需要先申请本文件全部项目。

日期：2026-10-21。用途：请 PM 转交 Databricks 平台、数据负责人及公司 IT。

## 申请结论

先申请下面 **6 项交互接入条件**；**后台创建者委托和公司邮件授权另列 2 项**，可并行确认。先复用现有资源，不申请迁移 Genie 知识、重建 Agent、Supervisor、第二个 Warehouse 或修改业务表。

当前个人开发环境的共享试用使用开发者身份，访客隔离暂时关闭。这不改变公司正式接入方案：业务调用按公司用户/任务创建者权限执行，元数据服务身份只存取任务记录。

## 已知目标

- Workspace：`https://adb-7405618086077365.5.azuredatabricks.net`
- Genie One MCP：`/ai-gateway/mcp-services/system.ai.genie_one_mcp`
- SQL Warehouse：`dydf_dev_sqlwh`，ID `540a674067e832d2`
- SQL REST：`/api/2.0/sql/statements/`
- 建议任务专用 Schema：`dev_catalog_aimlp.business_ai_hub`，**待平台批准/创建**。
- 试点组建议名：`business-ai-hub-pilot-users`，**仅建议名，尚未创建**。
- 元数据服务主体建议名：`business-ai-hub-metadata-sp`，**仅建议名，先核对能否复用已有合适身份**。

以下“未确认”不代表资源一定不存在；现有检查身份不是管理员，有些资源与 ACL 无权读取。

## 第一阶段：6 项交互接入条件

| 编号 | 授权/配置 | 具体请求 | 授予谁 | 负责人/交付物 |
|---|---|---|---|---|
| 1 | 内网服务器、HTTPS 与网络 | 一个获准运行 Python Web 服务的内网服务器/运行环境；站点域名和 TLS；浏览器能到站点及 Databricks SSO；服务器能以 HTTPS 443 到公司 Workspace 的 OAuth、MCP、SQL、模型 API；依赖通过公司批准源安装 | Hub 运行环境及试点用户 | IT：站点 origin、部署位置、网络可达结果。无需先开放个人临时隧道到公司 |
| 2 | 自定义用户 OAuth 应用 | 注册或复用 confidential Web OAuth client，Authorization Code + PKCE；回调严格为 `https://<批准的内网站点>/auth/callback`；核对下方 scopes | Hub 登录应用；用户各自同意授权 | Databricks 账户管理员：公开 Client ID、integration ID、批准回调/scopes、token 有效期；Secret 直接进服务器密钥系统 |
| 3 | 试点用户的业务使用权限 | Workspace 分配和所需 SQL access entitlement；Warehouse `540a674067e832d2` 的 CAN_USE；`system` USE CATALOG、`system.ai` USE SCHEMA、`system.ai.genie_one_mcp` EXECUTE；所用 Genie Agent 的 CAN RUN；所需业务 Catalog/Schema 的 USE 及表/视图/Metric Views 的 SELECT，保留现有行/列限制 | 确认后的试点用户组 | Warehouse 负责人、数据和 Agent 负责人：逐项确认有效权限。已存在的继承授权不重复申请；销售/竞争 V1/V2 中实际使用的版本由负责人确认 |
| 4 | 一个现有轻量模型的调用权限 | 提供一个兼容结构化输出的准确 endpoint/service ID。传统 Serving Endpoint：CAN_QUERY；Unity Gateway Model Service：服务 EXECUTE + 父级 USE CATALOG/USE SCHEMA。根据最终 API 路径确认 scopes | 试点用户组；无需给元数据身份 | 模型平台负责人：选用 ID 和权限确认；候选如 `databricks-gpt-oss-20b`，兼容性尚未实测，不新建模型 |
| 5 | 专用任务元数据 Schema 和两张表 | 经 Terraform/既定流程批准专用 Schema，创建 `business_task_registry_v1`、`business_task_runs_v1`。按已提供源码建表，不覆盖同名表；检查继承权限，不能让所有业务用户直接读取跨用户报告 | 准备阶段由平台管理员执行 | 平台/Terraform 维护者：批准的两张表完整名称、所有者。建表权限不必长期授给运行身份 |
| 6 | 专用元数据服务身份 | 分配到 Workspace；父 Catalog USE CATALOG、目标 Schema USE SCHEMA；仅上述两表 SELECT、MODIFY；Warehouse `540a674067e832d2` CAN_USE；配置 OAuth M2M 或公司批准的等价 SDK 身份 | 元数据服务主体 | 平台管理员：公开 Application/Client ID、Workspace 分配和 grants 清单；运行凭据直接配置到服务器 `business-ai-hub-storage` SDK profile/Secret 系统。不授业务表、Genie 或模型权限 |

### 第 2 项 OAuth scopes：请管理员逐个确认

当前代码配置的候选列表为：

| Scope | 用途 | 申请说明 |
|---|---|---|
| `iam.current-user:read` | 查询登录者真实身份，用于任务创建者绑定 | 当前代码需要；不能仅用客户端上报的邮箱代替 |
| `ai-gateway` | Unity Gateway Genie One MCP；选用 Model Service 路径时也需核对 | 应允许公司自定义 OAuth client 申请；仍需资源 EXECUTE |
| `sql:restricted-query` | 用户直接执行条件 SQL 的候选最小 SQL scope | 管理员须确认公司自定义 OAuth 注册和 Statement Execution API 是否支持；如必须使用 `sql`，先说明兼容原因，再调整应用配置 |
| `model-serving` | 传统 Serving Endpoint 的轻量模型调用 | 最终选择该路径才保留；Model Service 路径不能机械套用，按实际接口确认 |
| `offline_access` | 获取可刷新的用户凭据 | 确认有效期、刷新/撤销策略。允许此 scope 不代表后台长期授权存储已经实现 |

`openid/email/profile` 仅在选用 OIDC 身份功能或注册流程需要时按公司策略配置；现有后端身份绑定依赖真实 current-user 查询。不要直接默认申请 `all-apis`，也不要把 Databricks Apps 的配置方式不加核对地套用到内网自定义 Web 应用。

**OAuth scope 不会授予 Warehouse、MCP 或业务数据权限，上表第 3/4 项仍须单独满足。登录 OAuth client 与元数据服务主体是两套身份配置，不能互相代替。**

### 第 6 项最小数据授权范围

```sql
-- 仅作管理员审核模板；替换占位符后再决定是否执行。
GRANT USE CATALOG ON CATALOG `<catalog>` TO `<metadata-service-principal>`;
GRANT USE SCHEMA ON SCHEMA `<catalog>`.`<schema>` TO `<metadata-service-principal>`;
GRANT SELECT, MODIFY ON TABLE `<catalog>`.`<schema>`.`business_task_registry_v1` TO `<metadata-service-principal>`;
GRANT SELECT, MODIFY ON TABLE `<catalog>`.`<schema>`.`business_task_runs_v1` TO `<metadata-service-principal>`;
```

Warehouse CAN_USE 通过其权限界面/API 配置。MCP 服务授权也用其支持的界面/API，不编造 MCP_SERVICE 的 SQL GRANT 语法。业务用户通过 Hub 的任务权限访问元数据，不因使用 Hub 就直接获得两张共享表的 SELECT/MODIFY。

## 第二阶段：后台与报告发送的 2 项授权

| 编号 | 请求 | 具体范围 | 负责人 |
|---|---|---|---|
| 7 | 允许后台按任务创建者身份执行，并提供私有凭据保管能力 | 明确公司是否允许持久用户委托；提供公司批准的 Key Vault/凭据管理位置和仅限后台运行身份的存取权限，支持刷新令牌更新、撤销和失效后重新授权。不要使用报告中 users=MANAGE 的宽泛 Secret scope 保存用户令牌；不以共享业务 SP 兜底 | 安全团队、身份/平台管理员；代码侧还需完成 resolver 与执行器接入 |
| 8 | 公司发件服务授权 | 指定发件邮箱、允许的收件范围及网络出口；IT 选择 SMTP relay 或邮件 API，并提供最小发送权限。若选 Microsoft Graph，针对批准身份使用相应 Mail.Send 并限制发件邮箱；若选 SMTP，按公司的 relay/auth 政策配置。没有选定通道前，不同时申请多种邮件平台权限 | 邮件/IT 管理员；授权仅负责发信，不替代业务数据权限 |

当前代码已有 Gmail 适配器；如公司选择 SMTP 或 Microsoft Graph，仍需补对应发送适配器及正式入口。获得发送权限不代表这些代码已经完成。

若最终使用 Databricks Lakeflow Jobs，再单独确认负责部署者创建/管理该 Job 的权限、运营者对该 Job 的 CAN_MANAGE_RUN、批准的运行身份和计算配置。若由内网调度器触发，不为此额外申请 Databricks Job 权限。当前引擎含 Spark 依赖，调度方案及代码适配仍待确定；本清单不批准运行或启用调度。

## 请 PM 收回的非敏感结果

1. 第一阶段 6 项各自的负责人、批准/待定状态与时间。
2. 内网 HTTPS origin；OAuth Client ID、批准的回调与 scopes。
3. 试点组名称；Warehouse、MCP、Agent、数据和模型的有效权限确认。
4. 两张元数据表的完整名称；元数据服务主体公开 ID 和外部密钥配置位置。
5. 后台委托政策/密钥保管负责人；公司邮件通道和发件邮箱。

不要在回复、Markdown、ZIP 或 Git 中附密码、Access Token、Refresh Token、Client Secret、私钥。运行凭据由管理员直接交付公司密钥系统。

## 参考依据

- MCP 的 EXECUTE、父级 USE 与 Workspace 分配：https://learn.microsoft.com/en-us/azure/databricks/ai-gateway/govern-mcp-service
- 自定义 OAuth 注册：https://learn.microsoft.com/en-us/azure/databricks/integrations/enable-disable-oauth
- SQL 使用 Warehouse 的 CAN_USE：https://learn.microsoft.com/en-us/azure/databricks/sql/user/sql-editor/write-queries
- Genie Agent CAN RUN 与用户数据权限：https://learn.microsoft.com/en-us/azure/databricks/genie-agents/set-up
- Serving Endpoint CAN_QUERY：https://learn.microsoft.com/en-us/azure/databricks/machine-learning/model-serving/manage-serving-endpoints
- Model Service EXECUTE 与父级权限：https://kb.databricks.com/en_US/unity-catalog/migration-guide-moving-to-unity-ai-gateway
