import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from app.agents import prompts
from app.models import PromptExperiment, PromptVersion

BUILTINS = {"TopicBatch": prompts.TOPICS, "Article": prompts.ARTICLE, "CardPack": prompts.CARDS}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def snapshot(stage, settings, version=None):
    options = {
        "model": ((settings.llm_small_model if stage == "CardPack" else "") or settings.text_model),
        "temperature": settings.stage_temperatures.get(stage),
        "max_tokens": min(
            settings.stage_max_tokens.get(stage, settings.llm_max_tokens), settings.llm_max_tokens
        ),
        "model_locked": bool(version and version.options.get("model")),
        "routing_strategy": settings.routing_strategy,
        "routing_latency_budget_seconds": settings.routing_latency_budget_seconds,
        "routing_cost_budget": settings.routing_cost_budget,
    }
    instruction = BUILTINS[stage]
    if version:
        instruction = version.instruction
        options.update({k: v for k, v in version.options.items() if v is not None and v != ""})
    value = {
        "stage": stage,
        "instruction": instruction,
        "options": options,
        "provider": settings.llm_provider,
        "content_template_version": 1,
    }
    return {
        **value,
        "digest": digest(value),
        "version_id": version.id if version else "builtin",
        "name": version.name if version else "内置版本",
    }


async def owned_version(session, owner_id, version_id, stage=None):
    row = await session.get(PromptVersion, version_id)
    if row is None or row.owner_id != owner_id or (stage and row.stage != stage):
        raise HTTPException(404, "提示词版本不存在或阶段不匹配")
    return row


async def snapshots(session, owner_id, request_key, explicit, settings):
    result = {}
    for stage in BUILTINS:
        version, experiment = None, None
        if explicit.get(stage):
            version = await owned_version(session, owner_id, explicit[stage], stage)
        elif owner_id:
            experiment = await session.scalar(
                select(PromptExperiment).where(
                    PromptExperiment.owner_id == owner_id,
                    PromptExperiment.stage == stage,
                    PromptExperiment.enabled.is_(True),
                )
            )
            if experiment:
                bucket = (
                    int(
                        digest({"owner": owner_id, "key": request_key, "experiment": experiment.id})[:8],
                        16,
                    )
                    % 100
                )
                selected = (
                    experiment.treatment_id if bucket < experiment.percentage else experiment.control_id
                )
                version = await owned_version(session, owner_id, selected, stage)
            else:
                version = await session.scalar(
                    select(PromptVersion)
                    .where(
                        PromptVersion.owner_id == owner_id,
                        PromptVersion.stage == stage,
                        PromptVersion.active.is_(True),
                    )
                    .order_by(PromptVersion.created_at.desc())
                )
        value = snapshot(stage, settings, version)
        if experiment:
            value["experiment_id"] = experiment.id
            value["variant"] = "B" if version.id == experiment.treatment_id else "A"
        result[stage] = value
    return result


def prompt_for(state, stage):
    value = state.get("task_input", {}).get("prompt_snapshots", {}).get(stage)
    return (
        (value["instruction"], {**value["options"], "prompt_digest": value["digest"]})
        if value
        else (BUILTINS[stage], {})
    )
