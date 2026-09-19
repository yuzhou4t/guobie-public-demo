# 国别智枢公开体验

沿用国别智枢现有 Reader 页面、FastAPI API 与研究工作流。独立部署与数据库，不连接内部系统。

样本：刚果（金）6 个数据集的 419 条观测、326 份公开资料元数据（264 篇论文、51 条新闻、6 份报告、3 份政策及 2 份其他资料）、8 个已复核事件及 28 条来源关联、28 条已确认主张、54 条主体地点关联、17 个议题与 73 条资料标签关联、3 份原有公共田野示例。学术图表为精选样本的年度分布，不代表全部研究或全球热度，当年数据不完整。政策包括历史版本，应回源核对。无真实私人材料、论文摘要和正文。示例成果仅为既有界面演示，不代表真实模型本轮生成。

访客无需登录，各自使用临时项目。设置 `GUOBIE_PUBLIC_DEMO_SHARED_API_KEY` 时，评审可直接使用服务端 DeepSeek 通道，密钥不下发到浏览器；移除该变量后自动回退到访客输入自己的 DeepSeek 或兼容接口。模型仅在主动运行研究时调用，会话有效期最长一天；访客自带凭据的有效期最长一小时。过期凭据与会话由每日清理任务删除，清空本次体验会删除该会话的项目和记录。

本包不启用采集、账号管理、对外发布、后台任务或私人文件上传。内容版权归原来源，保留来源与许可说明。

## 部署

Python 3.11/3.12、PostgreSQL 16。设置 GUOBIE_DATABASE_URL 指向全新空库，GUOBIE_PUBLIC_DEMO_ENABLED=true、GUOBIE_AGENT_RUNTIME=user_api、GUOBIE_LIVE_SEARCH_PROVIDER=disabled、GUOBIE_PUBLIC_API_READ_ONLY=false。配置 GUOBIE_PUBLIC_DEMO_SECRET_KEY（Fernet）、GUOBIE_PUBLIC_DEMO_ALLOWED_ORIGINS（精确 HTTPS 域名）、GUOBIE_PUBLIC_DEMO_CRON_SECRET 与 CRON_SECRET（相同随机值）。评审期共享 DeepSeek 另设 `GUOBIE_PUBLIC_DEMO_SHARED_API_KEY`（Vercel Sensitive），默认使用 `https://api.deepseek.com` 的 `deepseek-flash` Responses 严格 JSON Schema（非思考模式）；评审结束后删除该环境变量并重新部署即可撤掉共享通道。生产使用 GUOBIE_ENVIRONMENT=production。

安装 requirements.txt 后在 backend 执行 `python -m alembic upgrade head`、`python -m app.cli.init_public_demo`。Vercel 入口 index.py。勿对内部数据库执行初始化。
