# 公司检查结果摘要

来源：用户提供的公司 Genie Code 只读检查报告（2026-10-21），本打包程序没有连接公司环境复核。

- 当前检查用户：`yulong.x.mao@haleon.com`，非 Workspace 管理员；无法查看不等于资源不存在。
- Host：`https://adb-7405618086077365.5.azuredatabricks.net`。
- Genie One MCP：`system.ai.genie_one_mcp`；当前用户经 account users 有父级 USE 和服务 EXECUTE。其他用户、OAuth scope 和策略/网络连通尚未验证。
- 已有四个 Agent：销售运营与竞争市场的 V1/V2。可见性不等于完整访问授权，也不能据此断言它们就是 Genie One 所有依赖。不迁移其知识或数据。
- Warehouse：`dydf_dev_sqlwh` / `540a674067e832d2`，Pro、非 serverless、2X-Small、15 分钟自动停止、Stopped。当前用户和未来元数据身份的 CAN_USE 未确认；负责人线索为 Carol Shen。先复用，无需把改 serverless 作为迁移前提。
- 可见可写 Schema：`dev_catalog_aimlp.dydf`，由 Terraform 管理并包含业务数据。建议由维护者评估专用 `dev_catalog_aimlp.business_ai_hub`，目前没有批准或创建。需检查继承 SELECT/MODIFY，避免其他用户能直接读取共享任务/报告表。
- 没有找到可确认复用的内网 Hub 自定义 OAuth 应用或专用元数据服务身份；需账户/平台管理员进一步核实，不代表它们一定不存在。
- 候选 chat endpoints：`databricks-claude-haiku-4-5`、`databricks-claude-haiku-5-5`、`databricks-gpt-oss-20b`、`databricks-meta-llama-3-1-8b-instruct`。报告称 READY；endpoint CAN_QUERY 和结构化输出未验证。Endpoint 名称不能与 `system.ai.*` Model Service ID 混用，两条 API 路径的权限需分别检查。
- 当前发现的 Azure Key Vault secret scope 为 `adb_dydf_kv_secret_scope`；报告显示 users=MANAGE。不要把用户长期令牌放入其中，先让安全团队审查并选择隔离的凭据方案。
- 用户创建者长期委托机制、公司发件服务尚未确认；这两项不阻挡先准备代码和交互式接入，但阻挡正式无人值守报告运行。

## 管理员最先确认的事项

1. 公司内网 HTTPS 站点地址、OAuth 回调地址，以及自定义 OAuth 应用的 Client ID。Client Secret 通过公司 Secret 管理交付给服务器，绝不返回提示词/ZIP。
2. 当前代码需要当前用户身份查询、Genie One、Statement Execution；如使用传统 Serving Endpoint，还需要相应模型调用授权。候选 scopes 为 `iam.current-user:read ai-gateway sql:restricted-query model-serving offline_access`，由管理员按实际 API 和公司支持范围确认；如果需要其他 SQL scope，需明确验证原因，不申请无关的 all-apis。
3. 试点用户组、Warehouse CAN_USE、MCP 及底层业务数据权限；只确认现有资源，不自动扩大授权。
4. 通过 Terraform/既定流程准备私有任务元数据 Schema 和专用服务身份；授予父级 USE、两张表 SELECT/MODIFY 以及执行元数据 SQL 的 Warehouse CAN_USE。建表权限属于准备阶段管理员，不默认长期授给运行身份。
5. 后续无人值守阶段的创建者授权存储、撤销、离职处理和失效后重新授权方式；不允许共享业务服务身份兜底。

参考文档（供管理员核对当前账户能力，不代表现场权限已验证）：
- https://learn.microsoft.com/en-us/azure/databricks/ai-gateway/govern-mcp-service
- https://learn.microsoft.com/en-us/azure/databricks/integrations/enable-disable-oauth
