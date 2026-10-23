# 公司电脑用 Git 同步与 Python 运行

2026-10-23。原ZIP目录先保留；Git首次克隆到新的文件夹，不在旧解压目录强行初始化/覆盖。

## 第一次（Windows PowerShell）

```powershell
cd $HOME\Documents
git clone https://github.com/lithoma/business-ai-hub.git business-ai-hub-git
cd business-ai-hub-git
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config/company-local.yaml config/company-local.private.yaml
.\.venv\Scripts\python.exe scripts/run-hub.py --config config/company-local.private.yaml
```

仓库是私有的。Git如弹出GitHub网页登录，使用具有仓库访问权限的GitHub账号；GitHub登录与公司Databricks登录是两回事。公司不允许个人GitHub账号/网站时继续用已提供的ZIP，不绕过公司网络策略，不把密码或Token发给聊天工具。

网页为 http://127.0.0.1:8765/ 。Databricks由Python SDK打开浏览器登录公司Workspace，不需要另装Databricks CLI。公司模型名称填到私有配置副本的TASK_PARSER_ENDPOINT；为空时可手动建草稿。

私有配置副本被Git忽略，更新不会直接覆盖它；模板变化后按新版字段合并。不要在其中保存密码/Token。Task/报告库在本机独立状态目录，不在仓库里。

## 后续更新

先关闭运行服务的终端，再在Git项目目录执行：

```powershell
git status --short
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts/run-hub.py --config config/company-local.private.yaml
```

若status有你自己的源码改动或pull提示冲突，先保留改动并核对，不使用reset --hard或强制覆盖。若运行公司服务使用8765，停止旧ZIP目录启动的实例后再启动Git版本。

## 这个仓库如何保持Python直接可运行

只跟踪经过构建的frontend/dist发布网页，因此公司电脑不用Node。node_modules、Python虚拟环境、登录缓存、数据库、邮件、日志和ZIP继续忽略。前端开发者修改页面后需重新构建并同步dist；普通使用者不执行npm。

单人localhost模式不提供多人OBO，也不自动开启后台Job/邮件。后台Job→Apps接口仍需平台部署验收；改为内网多人/长期托管时另按架构接入。
