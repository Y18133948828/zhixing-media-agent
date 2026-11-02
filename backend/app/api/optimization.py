from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.agents import prompts
from app.api.routes import idempotency_key, workflow
from app.core.security import auth, require_account, require_admin
from app.models import (
    EvaluationCase,
    EvaluationRun,
    GenerationRating,
    Knowledge,
    KnowledgeChunk,
    PromptExperiment,
    PromptVersion,
    User,
)
from app.schemas.content import KnowledgeCreate
from app.schemas.optimization import (
    CaseCreate,
    EvaluationCreate,
    ExperimentCreate,
    KnowledgeImport,
    PromptCreate,
    RatingCreate,
    UserRolePatch,
)
from app.services.knowledge_service import KnowledgeService, import_article
from app.services.prompt_store import BUILTINS, digest, owned_version, snapshot

router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])


def prompt_view(row):
    return {
        "id": row.id,
        "stage": row.stage,
        "name": row.name,
        "instruction": row.instruction,
        "options": row.options,
        "digest": row.digest,
        "active": row.active,
        "created_at": row.created_at,
    }


@router.get("/prompts", tags=["提示词"])
async def list_prompts(request: Request):
    owner = require_account(request)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(
                select(PromptVersion)
                .where(PromptVersion.owner_id == owner)
                .order_by(PromptVersion.created_at.desc())
                .limit(200)
            )
        ).all()
    return {
        "builtins": [snapshot(stage, request.app.state.settings) for stage in BUILTINS],
        "items": [prompt_view(r) for r in rows],
    }


@router.post("/prompts", status_code=201, tags=["提示词"])
async def create_prompt(body: PromptCreate, request: Request):
    owner = require_account(request)
    instruction = (
        body.instruction
        if body.instruction.startswith(prompts.BASE)
        else prompts.BASE + "\n" + body.instruction
    )
    options = body.options.model_dump(exclude_unset=True)
    async with request.app.state.db.sessions() as session, session.begin():
        # Owner row lock serializes releases/rollbacks and avoids two active versions.
        await session.scalar(select(User).where(User.id == owner).with_for_update())
        if body.activate:
            await session.execute(
                update(PromptVersion)
                .where(PromptVersion.owner_id == owner, PromptVersion.stage == body.stage)
                .values(active=False)
            )
        row = PromptVersion(
            owner_id=owner,
            stage=body.stage,
            name=body.name,
            instruction=instruction,
            options=options,
            digest=digest({"instruction": instruction, "options": options, "stage": body.stage}),
            active=body.activate,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return prompt_view(row)


@router.post("/prompts/{version_id}/activate", tags=["提示词"])
async def activate_prompt(version_id: str, request: Request):
    owner = require_account(request)
    async with request.app.state.db.sessions() as session, session.begin():
        await session.scalar(select(User).where(User.id == owner).with_for_update())
        row = await owned_version(session, owner, version_id)
        await session.execute(
            update(PromptVersion)
            .where(PromptVersion.owner_id == owner, PromptVersion.stage == row.stage)
            .values(active=False)
        )
        row.active = True
        return {
            "active": row.id,
            "stage": row.stage,
            "note": "仅影响之后创建的任务，运行中任务保留原快照",
        }


@router.get("/prompt-experiments", tags=["提示词"])
async def experiments(request: Request):
    owner = require_account(request)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(select(PromptExperiment).where(PromptExperiment.owner_id == owner))
        ).all()
        return [
            {
                "id": r.id,
                "stage": r.stage,
                "control_id": r.control_id,
                "treatment_id": r.treatment_id,
                "percentage": r.percentage,
                "enabled": r.enabled,
            }
            for r in rows
        ]


@router.put("/prompt-experiments", tags=["提示词"])
async def save_experiment(body: ExperimentCreate, request: Request):
    owner = require_account(request)
    if body.control_id == body.treatment_id:
        raise HTTPException(422, "A/B两个版本必须不同")
    async with request.app.state.db.sessions() as session, session.begin():
        await owned_version(session, owner, body.control_id, body.stage)
        await owned_version(session, owner, body.treatment_id, body.stage)
        data = body.model_dump()
        await session.execute(
            insert(PromptExperiment)
            .values(owner_id=owner, **data)
            .on_conflict_do_update(
                index_elements=[PromptExperiment.owner_id, PromptExperiment.stage], set_=data
            )
        )
        return {"saved": True, **data}


@router.put("/tasks/{task_id}/rating", tags=["效果评测"])
async def rate(task_id: str, body: RatingCreate, request: Request):
    owner = require_account(request)
    task = await workflow(request).get(task_id, write=True)
    if task.status != "READY":
        raise HTTPException(409, "图文就绪后可评价生成效果")
    data = {
        **body.model_dump(),
        "owner_id": owner,
        "prompt_snapshot": task.input.get("prompt_snapshots", {}),
        "result_snapshot": {
            "article": task.snapshot.get("approved_article"),
            "cards": task.snapshot.get("xhs_pack"),
        },
    }
    async with request.app.state.db.sessions() as session, session.begin():
        await session.execute(
            insert(GenerationRating)
            .values(task_id=task_id, **data)
            .on_conflict_do_update(index_elements=[GenerationRating.task_id], set_=data)
        )
    return {"saved": True, **body.model_dump()}


@router.get("/tasks/{task_id}/rating", tags=["效果评测"])
async def get_rating(task_id: str, request: Request):
    await workflow(request).get(task_id)
    async with request.app.state.db.sessions() as session:
        row = await session.get(GenerationRating, task_id)
        return (
            {"score": row.score, "dimensions": row.dimensions, "comment": row.comment} if row else None
        )


@router.post("/evaluation-cases", status_code=201, tags=["效果评测"])
async def create_case(body: CaseCreate, request: Request):
    owner = require_account(request)
    task = await workflow(request).get(body.task_id, write=True)
    if not task.snapshot.get("approved_article"):
        raise HTTPException(409, "请先完成文案审核")
    context = {
        "task_input": task.input,
        "selected_topic": task.snapshot["selected_topic"],
        "current_article": None,
        "human_feedback": "",
        **task.snapshot.get("context", {}),
    }
    async with request.app.state.db.sessions() as session, session.begin():
        row = EvaluationCase(
            owner_id=owner,
            task_id=task.id,
            name=body.name,
            context=context,
            expected=task.snapshot["approved_article"],
            rules=body.rules.model_dump(),
        )
        session.add(row)
        await session.flush()
        return {"id": row.id, "name": row.name, "rules": row.rules}


@router.get("/evaluation-cases", tags=["效果评测"])
async def list_cases(request: Request):
    owner = require_account(request)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(
                select(EvaluationCase)
                .where(EvaluationCase.owner_id == owner)
                .order_by(EvaluationCase.created_at.desc())
                .limit(100)
            )
        ).all()
        return [
            {
                "id": r.id,
                "task_id": r.task_id,
                "name": r.name,
                "rules": r.rules,
                "expected": r.expected,
                "created_at": r.created_at,
            }
            for r in rows
        ]


@router.post("/evaluations", status_code=202, tags=["效果评测"])
async def start_evaluation(
    body: EvaluationCreate, request: Request, key: str = Depends(idempotency_key)
):
    owner = require_account(request)
    scoped = digest({"owner": owner, "key": key, "action": "evaluate"})
    request_hash = digest(body.model_dump())
    async with request.app.state.db.sessions() as session:
        existing = await session.scalar(select(EvaluationRun).where(EvaluationRun.request_key == scoped))
        if existing:
            if existing.request_hash != request_hash:
                raise HTTPException(409, "幂等键已用于其他评测")
            return {"id": existing.id, "status": existing.status}
        rows = (
            await session.scalars(
                select(EvaluationCase).where(
                    EvaluationCase.id.in_(set(body.case_ids)), EvaluationCase.owner_id == owner
                )
            )
        ).all()
        if len(rows) != len(set(body.case_ids)):
            raise HTTPException(404, "评测样本不存在")
        if len(body.prompt_ids) != len(set(body.prompt_ids)):
            raise HTTPException(422, "对照评测请选择两个不同的版本")
        variants = [
            snapshot(
                "Article",
                request.app.state.settings,
                None if pid == "builtin" else await owned_version(session, owner, pid, "Article"),
            )
            for pid in body.prompt_ids
        ] or [snapshot("Article", request.app.state.settings)]
        row = EvaluationRun(
            owner_id=owner,
            request_key=scoped,
            request_hash=request_hash,
            cases=[
                {
                    "id": r.id,
                    "name": r.name,
                    "task_id": r.task_id,
                    "context": r.context,
                    "rules": r.rules,
                    "expected": r.expected,
                }
                for r in rows
            ],
            variants=variants,
            results=[],
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            raise HTTPException(409, "评测提交正在处理，请刷新") from None
        return {"id": row.id, "status": row.status, "planned_generations": len(rows) * len(variants)}


@router.get("/evaluations", tags=["效果评测"])
async def evaluations(request: Request):
    owner = require_account(request)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(
                select(EvaluationRun)
                .where(EvaluationRun.owner_id == owner)
                .order_by(EvaluationRun.created_at.desc())
                .limit(50)
            )
        ).all()
        return [
            {
                "id": r.id,
                "status": r.status,
                "results": r.results,
                "error": r.error,
                "variants": [{"name": v["name"], "digest": v["digest"]} for v in r.variants],
                "created_at": r.created_at,
            }
            for r in rows
        ]


@router.get("/knowledge/search", tags=["知识检索"])
async def search(request: Request, category: str, query: str = Query(min_length=1, max_length=2000)):
    return await KnowledgeService(request.app.state.db).search(request.state.owner_id, category, query)


async def own_knowledge(session, owner, knowledge_id):
    row = await session.get(Knowledge, knowledge_id)
    if not row or row.owner_id != owner:
        raise HTTPException(404, "素材不存在")
    return row


@router.put("/knowledge/{knowledge_id}", tags=["知识检索"])
async def edit_knowledge(knowledge_id: str, body: KnowledgeCreate, request: Request):
    async with request.app.state.db.sessions() as session:
        row = await own_knowledge(session, request.state.owner_id, knowledge_id)
    expected = {k: getattr(row, k) for k in ("title", "body", "category", "source_url")}
    replacement = body.model_dump(mode="json")
    for key, value in replacement.items():
        setattr(row, key, value)
    result = await KnowledgeService(request.app.state.db).index(
        row, replacement=replacement, expected=expected
    )
    return {"id": row.id, **result}


@router.post("/knowledge/{knowledge_id}/index", tags=["知识检索"])
async def reindex(knowledge_id: str, request: Request):
    async with request.app.state.db.sessions() as session:
        row = await own_knowledge(session, request.state.owner_id, knowledge_id)
    return await KnowledgeService(request.app.state.db).index(row)


@router.delete("/knowledge/{knowledge_id}", tags=["知识检索"])
async def remove_knowledge(knowledge_id: str, request: Request):
    async with request.app.state.db.sessions() as session, session.begin():
        row = await own_knowledge(session, request.state.owner_id, knowledge_id)
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == row.id))
        await session.delete(row)
    return {"deleted": True, "note": "已创建任务的素材快照保留"}


@router.post("/knowledge/import-url", status_code=201, tags=["知识检索"])
async def import_url(body: KnowledgeImport, request: Request):
    data = await import_article(body.url)
    if body.title:
        data["title"] = body.title
    async with request.app.state.db.sessions() as session, session.begin():
        row = Knowledge(owner_id=request.state.owner_id, category=body.category, **data)
        session.add(row)
        await session.flush()
    result = await KnowledgeService(request.app.state.db).index(row)
    return {"id": row.id, **result}


@router.get("/reviewers", tags=["审核协作"])
async def reviewers(request: Request):
    require_account(request)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(
                select(User)
                .where(User.disabled.is_(False), User.role.in_(["manager", "director", "admin"]))
                .limit(100)
            )
        ).all()
        return [{"id": u.id, "username": u.username, "role": u.role} for u in rows]


@router.get("/admin/users", tags=["账号管理"])
async def users(request: Request):
    require_admin(request)
    async with request.app.state.db.sessions() as session:
        rows = (await session.scalars(select(User).order_by(User.created_at).limit(200))).all()
        return [
            {"id": r.id, "username": r.username, "role": r.role, "disabled": r.disabled} for r in rows
        ]


@router.patch("/admin/users/{user_id}", tags=["账号管理"])
async def update_user(user_id: str, body: UserRolePatch, request: Request):
    require_admin(request)
    if user_id == request.state.owner_id and (body.role != "admin" or body.disabled):
        raise HTTPException(409, "不能移除自己的管理员权限或停用自己")
    async with request.app.state.db.sessions() as session, session.begin():
        row = await session.get(User, user_id)
        if not row:
            raise HTTPException(404, "账号不存在")
        row.role, row.disabled = body.role, body.disabled
        row.token_version += 1
    return {"updated": True, "sessions_revoked": True}
