# 申请一：Business AI Hub 本机内网试点必要条件

日期：2026-10-21。目标：在公司内网电脑运行现有系统，连接公司已有 Databricks；不新建云端应用、Agent 或计算资源。

## 运行方式

- 网页、API、任务记录和报告历史运行/保存在公司电脑。
- 任务存储先用本机 SQLite。SQLite 通常由标准 Python 的 sqlite3 模块提供，不需要安装数据库服务器，也不要求公司先建 Unity Catalog 任务表。
- 业务数据分析、条件 SQL 与报告分析连接已有 Databricks 资源。
- 用一个公司批准的现有账号登录，试点人员共用该账号的数据范围，暂不做访客之间的数据隔离。

可在公司电脑运行以下命令确认 SQLite 支持，不访问 Databricks：

```powershell
py -3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

若输出版本号即可使用；若没有 Python 或 sqlite3 模块，由 IT 提供带 sqlite3 的批准版 Python 3.11+。

## 一、电脑及登录条件

| 条件 | 最小要求 |
|---|---|
| 软件 | 允许运行 Python 3.11+、项目依赖和 Databricks CLI；使用公司批准的安装源 |
| 本机文件 | 可在批准的文件夹写入 SQLite、报告及日志 |
| 网络 | 电脑可访问公司 Workspace 的 OAuth、Genie One MCP、SQL REST、模型 API，以及公司 SSO |
| 登录 | 允许批准的现有公司账号通过现有 CLI/SDK OAuth 登录；现有客户端被禁用时，请管理员确认公司允许的等价方式 |

目标 Workspace：`https://adb-7405618086077365.5.azuredatabricks.net`。
候选运行账号：`yulong.x.mao@haleon.com`，由 PM/IT 确认是否可作为试点统一运行身份。

## 二、只核对并补齐三个现有资源的使用权限

| 资源 | 权限 | 备注 |
|---|---|---|
| Genie One 及现有业务资产 | `system` 的 USE CATALOG；`system.ai` 的 USE SCHEMA；`system.ai.genie_one_mcp` 的 EXECUTE；实际所用 Genie Agent 的 CAN RUN；相关业务表/视图/Metric Views 的父级 USE 和 SELECT | 当前检查账号的 MCP 和父级权限已确认；实际 Agent/数据访问及 API 连通仍需核对。只补缺项，不申请业务表写入 |
| SQL Warehouse | `dydf_dev_sqlwh` / `540a674067e832d2` 的 CAN_USE，及所需 Databricks SQL entitlement | Warehouse 已有，无需在电脑安装；可见不等于已有使用权限 |
| 一个轻量模型 | 选择现有兼容模型：传统 Serving Endpoint 的 CAN_QUERY；或者 Model Service 的 EXECUTE 和父级 USE，二选一 | 仅用于分流和任务表单提取。候选 `databricks-gpt-oss-20b`，其实际调用权限和结构化输出待核对，不申请新建模型 |

这批权限可支持自然语言提问、任务草稿、条件 SQL 检查及手动报告。各项能力仍需代码适配和现场验证，不能只根据列表可见性认定调用成功。

## 三、请返回的结果

1. 批准的公司运行账号，以及允许的登录方式。
2. Genie One/所用 Agent/业务数据可访问的确认。
3. Warehouse CAN_USE 确认。
4. 一个可调用模型的准确 endpoint/service ID。
5. 软件、网络和本机文件写入是否满足。

已有权限不重复申请。此阶段不要求 Workspace/Account 管理员角色、Web OAuth 注册、专用服务主体、云端任务 Schema/表、Key Vault、Databricks App、Job 或新 Warehouse。

邮件与无人值守调度不作为这份首轮申请的前置条件，见第二份可选清单。内网单账号公司启动方式仍需代码侧接入；原 ZIP 的 company.yaml 是较完整的 OAuth/云表模式，不能删掉必填项直接当成本机模式使用。

所有凭据在公司电脑/批准的密钥位置配置，不通过邮件、Markdown、ZIP 或 Git 交付。
