# 两个环境，一次选择

2026-10-23：公司电脑已能安装Python时，单人试用优先选择 `company-local.yaml`，启动命令为 `python scripts/run-hub.py --config config/company-local.yaml`。SDK浏览器登录公司Workspace，无需另装CLI；Task/报告保存本机独立SQLite。解析模型暂为空，填已批准公司模型后才可自然语言生成。它不是多人权限或长期后台托管入口。多人内网和Apps路径分别保留 `company.yaml`、`company-apps.yaml`。最新步骤见 `docs/company-transfer/START-HERE.md`。

- `local.yaml`：保留现有个人 Workspace、SQLite 测试库、模拟日期和手动试运行。
- `company.yaml`：公司 Azure Host/Warehouse 已填；Catalog/Schema、站点地址、OAuth Client ID 等待确认。缺少必填项时停止，不回退到测试资源。

在项目根目录启动，只改变配置文件路径：

```bash
python scripts/run-hub.py --config config/local.yaml
python scripts/run-hub.py --config config/company.yaml
```

只检查配置（不登录、不查询、不启动 Warehouse）：

```bash
python scripts/run-hub.py --config config/local.yaml --check-config
python scripts/run-hub.py --config config/company.yaml --check-config
```

公司文件尚未填完时，检查命令会列出缺失项并返回状态 2，这是预期行为。
非敏感配置以所选文件为准；密码、Token、OAuth Client Secret、Gmail Refresh Token 和签名密钥不能写入这两个文件，仍使用 SDK 凭据配置或外部 Secret Store。

Hub 和 Task Manager 共用 `HUB_CONFIG_FILE` 指定的绝对路径；Job 入口也支持这个变量，但不会因此启用执行或调度。公司服务器使用独立进程，不用本地 `dev-local.py` 启动器。现有本地服务无需现在切换或重启。

这两个文件只解决配置选择，不会自动迁移数据、权限、OAuth 注册、后台长期授权或正式试运行/邮件路由。公司一般运行模式下的手动报告/发信入口，仍须按迁移方案接入。
