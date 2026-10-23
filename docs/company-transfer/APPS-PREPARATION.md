# 公司 Apps 离线准备（暂不部署）

2026-10-22：先完成现有业务功能与页面验收，再生成最终传输包及部署。当前文件是部署草稿，不代表已取得公司资源权限。

- 保留 `config/local.yaml`。新增 `config/company-apps.yaml`，单独选择 Apps 模式。
- `scripts/run-app.py --check-config` 只检查文件与配置，不连 Databricks；当前缺项返回 2，属预期。
- 2026-10-23 更新：公司配置改为 `databricks_app_obo`。Apps 平台 SSO + 用户令牌处理业务查询；平台 App 身份仅处理任务元数据。原个人 OAuth 与显式旧共享模式保留，本地试用配置不变。
- OBO 模式只能置于 Databricks Apps 的受保护入口后；不能把手填转发头的公共服务器当作真实平台身份入口。
- 任务/报告写配置的 Delta 表，不使用 Apps 临时磁盘持久保存业务状态。修正了运行记录必须包含 attempt_id 的兼容问题。
- 默认关闭业务模型、SQL 工具、审核启用、试运行、定时器和邮件。最终逐项验收后开启，不随 App 启动自动运行。
- 默认不读取个人开发环境的 YAML 示例任务，不迁移 Genie 知识或创建 Supervisor。

未来 App 的部署根目录必须包含：

```text
app.yaml                 ← deployment/databricks-apps/app.yaml
requirements.txt         ← deployment/databricks-apps/requirements.txt（合并依赖）
scripts/run-app.py
config/company-apps.yaml
business-ai-hub/          ← 后端及 frontend/dist、前端源码
business-task-manager-v1/business-task-manager-v1/  ← 引擎和模板
```

只上传 `business-ai-hub/` 子目录会缺引擎和模板。最终 ZIP 在功能确认后重新生成；旧 ZIP 不会自动包含本轮修改。

公司批准后需要填：平台分配的 App HTTPS 地址、获批 catalog/schema 与两张表全名、一个已有轻量模型名称。Workspace 和 Warehouse 已填；它们对用户及元数据 App 身份的实际权限仍未验证。不填个人 SDK profile、不打包密码或 Token，无需另设站内 OAuth 登录应用。

需要在 Apps 的 User authorization 配置用户 scope；候选配置已保存于 `deployment/databricks-apps/user-authorization.json`：`iam.current-user:read`、`ai-gateway`、`sql:restricted-query`、`model-serving`。这不是 app.yaml 自动生效的字段，必须由有权限者在平台 UI/Apps API 配置并经用户同意。MCP 使用 ai-gateway；不额外申请旧 Genie Space/Jobs 管理权限。实际资源 EXECUTE、Warehouse CAN_USE 和数据 SELECT/行列策略仍需按用户授予。

App 服务主体仅需元数据表 SELECT/MODIFY、父级 USE 和运行元数据 SQL 所需 Warehouse CAN_USE；不要以授予 App 全部业务表权限来替代 OBO。

Task 自动绑定创建者，无逐任务授权步骤。当前每次已验证请求会更新短期内存凭据（上限 10 分钟）；后台执行前再次校验实际用户。长期离线执行仍需公司批准的可刷新凭据代理，通过 `AuthManager.creator_resolver(task)` 接入；不存在时不宣称可以每天无人值守运行。默认定时器保持关闭。详见 `docs/OBO-AND-TASK-IDENTITY.md`。

**后续实现更新：** 首选后台方案已改为原生 Job Run as 创建者，每次运行通过 Notebook 凭据换取 App-audience OAuth 令牌并调用原 App 执行器。无需用户在线或另建用户 refresh-token 库；代码和离线验证已完成，公司令牌交换/真实运行仍待验收。见 `docs/CREATOR-JOBS.md`。`HUB_CREATOR_JOBS_ENABLED=false`、空的 Job 绑定文件及 PAUSED 模板保持默认；不会自动创建或启动资源。上段凭据代理只保留为替换接口。

SQL 条件测试凭证跨重启使用时，还需通过平台 Secret 资源注入稳定的 `TASK_SQL_REVIEW_SECRET`；不能把个人本地凭证复制过去。邮件授权单独处理。

依据：[Apps 身份](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/auth)、[平台环境](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/system-env)、[代理登录头](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/http-headers)、[部署根目录 app.yaml](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/app-runtime)。离线检查不替代公司现场权限与连通验收。
