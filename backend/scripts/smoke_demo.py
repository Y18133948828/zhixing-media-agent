"""Explicitly automated demo: select, reject once, approve, then export via real HTTP."""

import argparse
import asyncio
import json
import time
import zipfile
from pathlib import Path
from uuid import uuid4

import httpx

from app.core.config import BACKEND_ROOT, Settings


async def main():
    parser = argparse.ArgumentParser(
        description="自动模拟人工操作，验证本地 Demo 全流程；请勿用于真实待审内容"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--allow-paid", action="store_true", help="允许自动测试调用已配置的付费模型")
    parser.add_argument("--output", type=Path, default=BACKEND_ROOT.parent / "示例成果")
    args = parser.parse_args()
    settings = Settings()
    if (settings.llm_provider != "mock" or settings.image_provider != "template") and not args.allow_paid:
        parser.error("当前是真实模型配置；本脚本会自动选择/审核测试任务并产生用量，需要显式 --allow-paid")
    args.output.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        base_url=args.base_url, headers={"X-API-Key": settings.api_key.get_secret_value()}, timeout=30
    ) as client:

        async def post(path, body):
            response = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
            response.raise_for_status()
            return response.json()

        async def wait(task_id, expected, minimum_version=0):
            until = time.monotonic() + 300
            while time.monotonic() < until:
                response = await client.get(f"/api/v1/tasks/{task_id}")
                response.raise_for_status()
                task = response.json()
                if task["status"] == "FAILED":
                    raise RuntimeError(task["last_error"])
                if (
                    task["status"] == expected
                    and task["state"].get("article_version", 0) >= minimum_version
                ):
                    return task
                await asyncio.sleep(0.3)
            raise TimeoutError(f"等待 {expected} 超时")

        task = await post(
            "/api/v1/tasks",
            {"course_category": "AI应用开发", "target_audience": "零基础学习者", "card_count": 4},
        )
        task_id = task["id"]
        print(f"创建任务：{task_id}")
        task = await wait(task_id, "WAITING_TOPIC_SELECTION")
        print(f"候选选题：{len(task['state']['candidate_topics'])} 个")
        await post(
            f"/api/v1/tasks/{task_id}/select-topic",
            {
                "interrupt_id": task["interrupt_id"],
                "topic_id": task["state"]["candidate_topics"][0]["id"],
                "operator": "自动演示脚本",
            },
        )
        task = await wait(task_id, "WAITING_ARTICLE_REVIEW", 1)
        await post(
            f"/api/v1/tasks/{task_id}/review",
            {
                "interrupt_id": task["interrupt_id"],
                "article_version": 1,
                "decision": "rejected",
                "feedback": "开头更通俗，强调小实验和人工核验",
                "operator": "自动演示脚本",
            },
        )
        task = await wait(task_id, "WAITING_ARTICLE_REVIEW", 2)
        print("已打回重写，进入第 2 版审核。")
        await post(
            f"/api/v1/tasks/{task_id}/review",
            {
                "interrupt_id": task["interrupt_id"],
                "article_version": 2,
                "decision": "approved",
                "operator": "自动演示脚本",
            },
        )
        task = await wait(task_id, "READY", 2)
        result = await client.get(f"/api/v1/tasks/{task_id}/export")
        result.raise_for_status()
        archive_path = args.output / "演示图文包.zip"
        archive_path.write_bytes(result.content)
        with zipfile.ZipFile(archive_path) as archive:
            # Only our server's fixed, flat asset filenames may be extracted.
            for name in archive.namelist():
                if Path(name).name != name:
                    raise ValueError("导出包包含非法路径")
            archive.extractall(args.output)
        (args.output / "任务快照.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"模型模式：{task['state']['mode']}；图文包：{archive_path}")


if __name__ == "__main__":
    asyncio.run(main())
