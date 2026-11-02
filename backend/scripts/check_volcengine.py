"""Local config check by default; explicit flags make real, potentially billable API calls."""

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.agents import prompts
from app.core.config import BACKEND_ROOT, Settings
from app.core.llm_factory import LLM, ModelOutputError
from app.schemas.content import TaskCreate, TopicBatch
from app.services.image_engine import image_engine
from app.services.storage import Storage


async def run(args):
    text = args.live_text or not args.live_image
    image = args.live_image or not args.live_text
    try:
        settings = Settings(
            llm_provider="volcengine" if text else "mock",
            image_provider="volcengine" if image else "template",
        )
    except ValidationError as exc:
        print("配置尚未完成：" + "；".join(item["msg"] for item in exc.errors(include_input=False)))
        return 1
    print("方舟配置检查通过。密钥不输出。")
    if not args.live_text and not args.live_image:
        print("未发送网络请求。真实联调可加 --live-text 或 --live-image，将产生相应 API 用量。")
        return 0
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")
    output = BACKEND_ROOT.parent / "示例成果" / "火山方舟联调" / stamp
    if args.live_text:
        llm = LLM(settings)
        try:
            result = await llm.generate(
                TopicBatch,
                prompts.TOPICS,
                {"task_input": TaskCreate(topic_count=3).model_dump(), "has_live_trend_data": False},
            )
            output.mkdir(parents=True, exist_ok=True)
            (output / "选题联调.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
            print(
                f"文字接口通过：返回 {len(result.topics)} 个结构化选题，结果：{output / '选题联调.json'}"
            )
        finally:
            await llm.close()
    if args.live_image:
        pack = {
            "slides": [
                {
                    "title": "AI 编程先做好三个检查",
                    "subtitle": "接口联调测试卡片",
                    "bullets": ["明确输入内容", "检查输出结果", "保留人工审核"],
                    "code": "",
                    "takeaway": "用小任务验证完整流程",
                }
            ]
        }
        storage = Storage(output)
        files = await image_engine(settings, storage).render("图片测试", 1, pack, "clean", "live")
        print(f"生图接口通过：返回 {len(files)} 张图片，结果：{storage.resolve(files[0]['path'])}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="火山方舟配置检查；默认离线，显式 live 参数才调用计费接口"
    )
    parser.add_argument(
        "--live-text", action="store_true", help="真实请求文本 API，可能重试，产生 Token 用量"
    )
    parser.add_argument(
        "--live-image", action="store_true", help="真实生成一张知识卡，可能重试，产生生图用量"
    )
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except ModelOutputError as exc:
        print(f"联调失败：{exc}")
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"联调失败（{type(exc).__name__}）。请检查本地配置和网络；未输出响应正文或密钥。")
        raise SystemExit(1) from None
