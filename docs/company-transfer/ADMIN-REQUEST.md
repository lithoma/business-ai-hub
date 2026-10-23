# Business AI Hub 公司试点接入申请

请协助确认以下接入事项。本次先完成配置审核，不部署、启动 Warehouse、运行 Job 或启用邮件。我们复用已有 Genie One 和业务 Agent，不申请迁移知识或复制业务数据。

| 请求 | 请确认/提供 | 负责人建议 |
|---|---|---|
| 内网入口 | 试点服务器、HTTPS 域名及能否访问公司 Workspace 的 OAuth/SQL/MCP 地址 | 公司 IT |
| 用户 OAuth | 可复用或新注册的 confidential 自定义 OAuth 应用，支持 Authorization Code + PKCE；精确回调为 `https://<批准的内网站点>/auth/callback` | Databricks 账户管理员 |
| OAuth scopes | 按实际 API 核对当前用户身份、ai-gateway、SQL、可选模型和 offline_access 的授权范围；不默认授予 all-apis | 账户管理员/应用负责人 |
| Warehouse | 确认试点用户组及元数据服务身份对 `dydf_dev_sqlwh` / `540a674067e832d2` 的 CAN_USE | Warehouse 负责人 |
| 业务访问 | 确认试点用户可调用 Genie One MCP 及所需底层业务数据；保留用户原有行/列权限 | 数据/Agent 负责人 |
| 任务元数据 | 评估专用 Schema `dev_catalog_aimlp.business_ai_hub`，经 Terraform/既定流程创建两张表；检查继承权限隔离 | 平台/Terraform 负责人 |
| 元数据身份 | 确认现有可复用身份，否则准备专用身份；仅父级 USE、两表 SELECT/MODIFY、Warehouse CAN_USE 和 Workspace 分配，不增加业务表访问 | 平台管理员 |
| 轻量模型 | 从现有模型选一个获准、兼容结构化输出的服务，提供准确 endpoint/service ID；测试单独批准 | 模型平台负责人 |

OAuth 应用用途：内网 Hub 的用户登录和以该用户权限调用 Databricks。应用不会以元数据服务身份代替用户访问业务数据。请提供非敏感的 Client ID、批准的回调和 scope 列表；如生成 Client Secret，请直接配置到批准的服务器密钥系统，不发进邮件、聊天或 ZIP。

当前交互登录凭据只在服务器进程内存中保存，不承诺重启后或用户离线仍可运行。后续开启后台任务前，另请安全团队确认创建者长期授权的私有存储、刷新、撤销及失效时重新授权流程；不得使用 users 可广泛管理的 Secret scope 或共享业务身份兜底。后台执行代码仍需完成相应接入。

公司发件服务及授权在后续单独确认。请先回复各项“已有/可复用、需新建、当前无权确认”以及对应负责人；不要提供任何密码、Token 或 Secret 值。
