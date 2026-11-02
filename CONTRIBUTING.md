# 参与开发

先按[安装指南](docs/安装与配置.md)完成本地环境。提交问题时说明操作步骤、期望行为、实际结果和版本；日志请移除 Key、密码、令牌及个人资料。

代码分别位于 `backend/app` 与 `frontend/src`。数据库变更通过 Alembic 新建迁移，不修改已经发布的迁移历史。API 保持 `/api/v1` 数据契约，破坏性变更需明确说明。

测试使用独立 PostgreSQL `_test` 数据库；设置 `TEST_DATABASE_URL` 后运行 `python scripts/manage.py check`。测试不得调用真实付费服务，不得清空日常业务库。提交前运行 `python scripts/release_check.py`，检查待发布文件。

请保留第三方许可与来源声明。贡献的代码需具有可分发权，按本仓库 MIT License 提供。不要提交 `.env`、数据库转储、运行日志、生成的用户内容或原课程资料。
