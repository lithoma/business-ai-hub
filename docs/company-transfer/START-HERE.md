# Business AI Hub：先读这里

2026-10-23 最新传输包。当前优先在**公司电脑用 Python 单人试用**；Databricks Apps 配置与后台 Job 代码同时保留。ZIP不是已经部署好的公司系统。

## 1. 公司电脑 Python 运行（当前先试这一条）

需要 Python 3.11或更高版本（建议3.11/3.12）、公司批准的依赖源、浏览器，以及能访问公司 Databricks 的网络。不需要 Node、Docker、Git 或另装 Databricks CLI；前端已编译。

解压 ZIP，进入包含 requirements.txt 和 scripts 的根目录。Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/run-hub.py --config config/company-local.yaml --check-config
.\.venv\Scripts\python.exe scripts/run-hub.py --config config/company-local.yaml
```

macOS/Linux 用 .venv/bin/python 替代上面的 .venv\Scripts\python.exe。

浏览器打开 http://127.0.0.1:8765/ 。首次身份确认时 SDK 打开 Databricks 登录页面，使用公司账号登录。本机8020端口需可用作SDK OAuth回调。SDK把登录缓存保存在本机用户目录，不写进项目/ZIP，不需要把密码或Token交给聊天工具。

**务必选 config/company-local.yaml，不要选 config/local.yaml：后者是原个人开发Workspace。**

公司本机模式：

- 已填公司Workspace、Warehouse；业务统一Genie One，使用当前公司用户权限，不创建云端任务表。
- Task/报告存在独立SQLite：~/.local/share/business-ai-hub-company-demo/dev.sqlite3。Windows的~为当前用户目录。这不是公司多人共享库；请使用公司批准的本机存储位置。
- TASK_PARSER_ENDPOINT暂为空；手动草稿可用。自然语言分流/表单提取需要填入已批准的公司轻量模型名称，然后停止服务、重新启动。模型未配置不等于网页坏掉。
- 登录、MCP、SQL实际权限仍待公司验证。真正点击SQL可能唤起已停止的Warehouse；离线配置检查不登录、不查询。
- 不带走原电脑的Task/报告数据库、个人Gmail凭据。公司机邮件默认未配置；原电脑手动Demo发信不受影响。
- 仅监听127.0.0.1，所有请求使用当前单人身份，不是多人OBO入口；不要改成0.0.0.0来共享。
- 不启动定时器、原生Job或自动发信；关闭终端会停止服务。

依赖下载受限时，使用公司批准的镜像/离线依赖包，不关闭TLS校验。ZIP不含Python安装包、虚拟环境或wheel离线依赖。

## 2. 多人和长期任务：后续选择

| 场景 | 配置与边界 |
| --- | --- |
| 公司电脑单人先测试 | company-local.yaml；SDK浏览器登录，无需先注册自定义网站OAuth |
| 公司内网服务器多人访问 | company.yaml；需稳定HTTPS、个人OAuth应用及回调、受控后台身份 |
| Databricks Apps多人访问 | company-apps.yaml；平台SSO/OBO、App元数据表和用户scope；根目录app.yaml/requirements.txt已齐全 |
| 员工关网页后的周期任务 | 当前后台是原生Job Run as创建者→App令牌→App执行器，见docs/CREATOR-JOBS.md；不能回调公司电脑127.0.0.1。若最终只用内网服务器，需另接可达且受保护的后台身份入口，不能宣称已完成此链路 |

管理员批准前，不自动创建/覆盖表、部署App、运行Job或启用调度。不迁移旧Genie知识、Matrix/Metric Views或业务数据；复用公司现有Genie One。

## 3. 包内文件

- business-ai-hub/：后端、网页源码及编译网页。
- business-task-manager-v1/business-task-manager-v1/：执行器和报告/邮件组件；示例Task已在传输副本中禁用。
- config/：个人本机、公司本机、内网多人、Apps四种配置，不含凭据。
- deployment/：Apps启动和scope模板。
- docs/REVIEW-INDEX.md：详细测试建议、架构、示例代码入口。
- company-preparation/：公司检查、审批、Genie Code材料；历史材料以本页最新路径为准。
- database/：以后获批的持久任务库结构参考；不要原样执行旧workspace.douyin SQL。
- MANIFEST.json及ZIP旁的.sha256：文件版本、哈希、校验与传输副本改动。

不含密码、Token、SDK登录缓存、SQLite数据、报告/邮件、遥测日志、Git历史、node_modules、虚拟环境或旧Workspace知识。原电脑源码与运行服务保持原状。

公司Genie Code不能读取电脑Documents，除非你把源码放到它可访问的Workspace路径。需要它协助时，再提供实际路径与company-preparation/GENIE-CODE-PROMPT.txt；本机Python运行不需要先上传Workspace。
