from typing import Literal

from pydantic import Field, model_validator

from app.schemas.content import StrictModel

Stage = Literal["TopicBatch", "Article", "CardPack"]


class PromptOptions(StrictModel):
    model: str = Field("", max_length=200)
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=256, le=32000)


class PromptCreate(StrictModel):
    stage: Stage
    name: str = Field(min_length=1, max_length=100)
    instruction: str = Field(min_length=10, max_length=16000)
    options: PromptOptions = Field(default_factory=PromptOptions)
    activate: bool = False


class ExperimentCreate(StrictModel):
    stage: Stage
    control_id: str = Field(min_length=1, max_length=36)
    treatment_id: str = Field(min_length=1, max_length=36)
    percentage: int = Field(50, ge=0, le=100)
    enabled: bool = True


class RatingCreate(StrictModel):
    score: int = Field(ge=1, le=5)
    dimensions: dict[Literal["accuracy", "clarity", "usefulness", "style"], int] = Field(
        default_factory=dict
    )
    comment: str = Field("", max_length=2000)

    @model_validator(mode="after")
    def valid_dimensions(self):
        if any(not 1 <= value <= 5 for value in self.dimensions.values()):
            raise ValueError("各维度评分应在1–5之间")
        return self


class EvaluationRules(StrictModel):
    must_include: list[str] = Field(default_factory=list, max_length=20)
    must_avoid: list[str] = Field(default_factory=list, max_length=20)
    min_chars: int = Field(200, ge=0, le=15000)
    max_chars: int = Field(15000, ge=200, le=15000)

    @model_validator(mode="after")
    def limits(self):
        if self.min_chars > self.max_chars or any(
            not t or len(t) > 100 for t in [*self.must_include, *self.must_avoid]
        ):
            raise ValueError("评测规则长度或关键词不合法")
        return self


class CaseCreate(StrictModel):
    task_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=150)
    rules: EvaluationRules = Field(default_factory=EvaluationRules)


class EvaluationCreate(StrictModel):
    case_ids: list[str] = Field(min_length=1, max_length=10)
    prompt_ids: list[str] = Field(default_factory=list, max_length=2)


class KnowledgeImport(StrictModel):
    url: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=80)
    title: str = Field("", max_length=200)


class UserRolePatch(StrictModel):
    role: Literal["editor", "manager", "director", "admin"]
    disabled: bool = False
