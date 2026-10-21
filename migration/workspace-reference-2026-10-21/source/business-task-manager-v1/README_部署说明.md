# Business Task Manager V1 — 部署说明（不执行测试）

## 1. 本次交付内容

通用、配置驱动的 Python Task Manager：每天一次共享调度；daily/weekly 到期判断；`always`/`sql`/`agent` 三种触发；真实 Genie A1/A2 或可选 Supervisor 生成独立报告；Python 合并当日报告到 Delta Outbox；Job 内第二个 SQL Alert Task 读取 Outbox，向一个固定收件组发送一封汇总邮件。

**当前交付为代码 + 配置模板，不表示已部署、已接通所有接口或已完成测试。** 不会启动旧 M6 Job，不会发邮件。所有 example tasks 均 `enabled: false`，防止意外查询或发送。还需由 Genie Code 将现有已经验证的实际 KPI 检测逻辑移入 `tasks/sales_anomaly/trigger.sql` 并启用任务。

## 2. 唯一上传路径

将整个解压后的文件夹上传到 Databricks Workspace：

`/Workspace/Users/lithomas@wustl.edu/business-task-manager-v1/`

重要：**不要上传到旧 M6 的 `douyin-business-cockpit-agent/` 目录覆盖现有文件。** 这是独立新项目。

推荐在 Mac 上安装并登录 Databricks CLI 后，在解压目录的上一层执行：

```bash
# 下面命令只上传文件，不创建 Job、不执行测试。
databricks workspace import-dir ./business_task_manager_v1 \
  /Workspace/Users/lithomas@wustl.edu/business-task-manager-v1 --overwrite
```

也可以在 Workspace UI 创建上述文件夹、逐一上传文件和 `tasks` 子文件夹。ZIP **不能保证**直接以项目目录形式被 Workspace UI 解压；先在本地解压。

## 3. 文件说明

- `main.py`：唯一 Python Job 入口（Databricks Job 类型 `Python script`，源：Workspace）。
- `task_engine.py`：读取 YAML、判断 daily/weekly 和 always/sql/agent、统一生成报告。
- `agent_client.py`：已填写 A1/A2 Genie Space IDs；可选 Supervisor HTTP Adapter，需要补真实地址/路由/OAuth。
- `outbox.py`：`MERGE` 写入一个每日/收件组的 Delta outbox 行，没有 Alert 状态机。
- `settings.yaml`：Workspace、时区、Agent ID、邮件 Outbox。
- `tasks/<id>/config.yaml`：每个 Task 的开关/频率/触发方式/报告 Agent。
- `tasks/<id>/trigger.sql`：SQL 触发。必须返回恰好一行、类型为 BOOLEAN 的 `should_trigger`，`reason` 可选。
- `tasks/<id>/trigger_prompt.md`：Agent 触发，要求只返回 JSON `{ "trigger": true/false, "reason": "..." }`。
- `tasks/<id>/report_prompt.md` 与 `report_template.md`：报告内容与格式。
- `alert_outbox.sql`：**用于新建一个 SQL Alert V2** 的查询，固定收件组 `default`。
- `GENIE_CODE_SETUP_PROMPT.txt`：明确的 Genie Code 配置指令。

任务 SQL 可以使用 Spark SQL 参数标记 `:business_date`, `:period_start`, `:period_end`；9 点运行默认处理昨天的数据。Task Manager 是普通 Python 程序，不绑定任意特定 Databricks Job ID。

## 4. 已知 IDs（直接写入文件）

Workspace：`https://dbc-f19e9824-546f.cloud.databricks.com`

数据：`workspace.douyin`

Genie A1：`01f1c60941551d9b94f73515f09e4b7b`

Genie A2：`01f1c60944fa1a43bf79ff847cd5323d`

现有 Supervisor App 名称：`agent-bricks-douyin-cockpit`

旧 M6 Alert：`3526956768809863` — **仅作历史参考，不要复用或改动**（旧版 numeric ID API 多次失败）。

已知邮件 destination：`ea82f125-dac5-4eee-bf35-61a3ef4cdee1`（历史上对应 `tianyili397@gmail.com`，新 Alert UI 中需核对）。

## 5. Genie Code 需要且仅需要完成的 Workspace 配置

1. **真实 SQL 触发**：从旧 M6 的已验证实际 KPI 检测代码/SQL 提取业务判断逻辑，写入 `tasks/sales_anomaly/trigger.sql`，保证读取正确业务数据，而非只读取 `kpi_alert_reports` 通知表。暂时可使用 mock data，但必须清楚注明是 mock，不能称作生产数据。完成后启用 `sales_anomaly`（其他 3 个样例继续关闭）。如果原检测逻辑是纯 Python、不能直接复用成 SQL，或者不能确认真实字段，就报告缺失并保持关闭；禁止猜测或擅自大规模改写。
2. **Supervisor（可选但推荐）**：检查 App `agent-bricks-douyin-cockpit` 实际 HTTP 路径（`/responses` 或 `/api/invocations`）和 OAuth 认证。只有能在 Job identity 下提供 OAuth token 时，才填写 `settings.yaml` 的 `app_url/endpoint_path`，并将 sales_anomaly 的 `report.agent` 改为 `supervisor`。否则保持目前已知可用的 `genie_a1`，标注 Supervisor 尚未接通；**不要修改或重新部署旧 App**。Databricks Apps 不接受 PAT；需要 OAuth token 和合适的权限。当前 Adapter 使用环境变量 `DATABRICKS_APP_OAUTH_TOKEN`，不能把短期 token 硬编码到 YAML。生产使用时应确认 Job 的 OAuth 获取/刷新方式，否则暂不切换。
3. **新 Job：** `Business Task Manager V1`，两个顺序 Task：① `Python script` 指向本项目 `main.py`；② 新 SQL Alert V2 任务，依赖 Python task 成功。Python compute 必须支持 SparkSession；依赖 `databricks-sdk>=0.122.0`、PyYAML、requests。Job 最大并发 1、task retries 0。Job 每天 `09:00`、时区 `Asia/Shanghai`，**初始 PAUSED**；一个 Scheduler，不需要 24 个 Jobs。**不得使用旧 M6 Jobs**。
4. **新 Alert V2：** 名称 `Business Task Manager V1 - Outbox Email`，查询见 `alert_outbox.sql`。Condition 使用 `should_notify > 0`，收件人使用已有 destination（核实后），邮件正文包含 `{{QUERY_RESULT_TABLE}}`，`notify_on_ok=false`，`retrigger_seconds=1`（如果 UI 提供；配置受限则记录明确 blocker，绝不假定以后连续异常都能重复发邮件）。Alert 自身独立 Schedule 保持 PAUSED，**仅由新 Job 的第二个 Task 评估**。SQL Warehouse ID 须由 Genie Code 在 UI 中选取可访问的现有 warehouse。
5. 两个旧 M6 Jobs `154252282617018` / `480870378532889` 保持 PAUSED，旧 SQL Alert 保持不变。

## 6. 重要限制，不要掩盖

- 新 SQL Alert 仍有状态机制。`retrigger_seconds=1` 是连续天有异常时重复通知的关键配置；如果新 Alert 不支持或不能验证设置，请报告 BLOCKED，**不要**开发清理/重置状态机来绕过。
- 固定收件组、**每次 Job 最多一封合并报告邮件**；新增收件组或独立邮件主题需要其他 Alert 或未来的邮件 API。
- Outbox 的 `should_notify=true` 意味着“Python 已提交一批报告”，**不代表已发送/送达**。不能把 SQL Alert Job SUCCESS 等同于邮件收到了。
- `retrigger_seconds=1` 允许每次评估通知：手动重复评估当日报告可能重复发邮件。V1 采用每天一次 + no retries + max concurrent 1 控制风险；没有严格 exactly-once 邮件语义。
- `business_date_offset_days=1` 适合早上处理前一天数据；如果源表延迟更新，必须改用已完成的数据日期。
- Agent 判断采用 JSON Boolean 严格校验，非法响应会使 Job 失败，**不会**默认“正常”；不调用第二个 LLM 猜测。
- 从 Workspace 导入的源文件不包含或安装任何 OAuth 凭证。初始 Supervisor Adapter 没有 `app_url/endpoint_path`，不能宣称此接口已部署。

## 7. 本轮严格不做

不执行 Job、不打开 schedule、不发邮件、不跑全量三天模拟、不启动 Watchdog、不运行 Genie 进行测试、不修改旧 M6 系统。只完成文件上传与必要的配置建立，之后把 Job ID / 新 Alert ID / 未解决字段回报用户等待验收批准。
