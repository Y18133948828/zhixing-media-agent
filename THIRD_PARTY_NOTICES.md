# 第三方来源与致谢

本项目的原创实现以根目录 MIT License 提供。依赖及随项目分发的第三方文件仍适用各自许可证。

## 需求与架构参考

工作流需求及优化条目参考[《自媒体运营 AI 智能体》课程项目文档](https://jianxuanguan.feishu.cn/wiki/Tp4OwJxJgiJKjdkxgYVc9DzMnwc)。本仓库是在自有 Demo 上重新实现对应业务流程，不分发课程正文、下载的原仓库归档、登录态或原配置。参考源码 README 标注 MIT，下载包未包含独立 LICENSE；本项目未将该归档整体复制为本仓库。

本项目与参考课程、LangChain、火山引擎、小红书及微信均无官方隶属或背书关系。

## 随仓库分发的资源

- `backend/app/api/static/docs/`：Swagger UI 本地资源，来源及版本见该目录 `SOURCE.txt`；保留 Apache-2.0 `LICENSE` 与 `NOTICE`，内嵌组件的声明也保留在原文件中。
- 项目不分发商业字体、浏览器程序、PostgreSQL 程序或第三方模型权重。Playwright Chromium 由安装命令从其上游下载。

## 包管理依赖

Python 和前端依赖分别记录在 `backend/requirements.lock` 与 `frontend/package-lock.json`。安装工具从各自上游下载，未把 `.venv` 或 `node_modules` 打包为源码的一部分。第三方组件的许可应以实际安装版本的随附声明为准。
