# 项目审阅入口

2026-10-23。先看这一页，不必逐个翻源码。

| 你想看什么 | 文件 |
| --- | --- |
| 整个平台的详细测试建议；可直接给修改意见 | [PLATFORM-TEST-PLAN.md](PLATFORM-TEST-PLAN.md) |
| 下午会议怎么演示、在线 OBO 架构 | [OBO-AND-TASK-IDENTITY.md](OBO-AND-TASK-IDENTITY.md) 的 Demo 建议 |
| 关闭网页后 Task 如何继续按创建者权限运行 | [CREATOR-JOBS.md](CREATOR-JOBS.md) |
| 给技术同事看的身份隔离 example code | [examples/authorization](../examples/authorization/README.md) |
| 无云调用的原生 Job JSON 示例生成器 | [prepare-creator-job.py](../scripts/prepare-creator-job.py)，配置 [creator-job.example.json](../config/creator-job.example.json) |
| 公司 Apps 准备与待批准项 | [APPS-PREPARATION.md](company-transfer/APPS-PREPARATION.md) |
| 旧版真实测试证据及其适用范围 | [validation](validation)；历史通过不等于当前公司版已经通过 |

本轮不创建公司资源，不部署，不启动 Job，不发邮件，不生成最终 ZIP。当前本地试用配置保持原状。
