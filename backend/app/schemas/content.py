from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Source(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    url: HttpUrl | None = None
    excerpt: str = Field(min_length=1, max_length=4000)


class TaskCreate(StrictModel):
    course_category: str = Field("AI应用开发", min_length=1, max_length=80)
    target_audience: str = Field("零基础转行人群", min_length=1, max_length=200)
    brief: str = Field("提供可以实操验证的技术干货", min_length=1, max_length=2000)
    topic_count: int = Field(4, ge=3, le=5)
    card_count: int = Field(4, ge=3, le=5)
    theme: Literal["clean", "dark"] = "clean"
    content_template: Literal["tutorial", "comparison", "checklist"] = "tutorial"
    editor_notes: str = Field("", max_length=4000)
    sources: list[Source] = Field(default_factory=list, max_length=10)
    prompt_ids: dict[str, str] = Field(default_factory=dict, max_length=3)
    reviewer_ids: list[str] = Field(default_factory=list, max_length=2)
    review_topics: bool = False
    workflow_template: Literal["xhs_wechat", "xhs_only", "wechat_only"] = "xhs_wechat"


class Topic(StrictModel):
    id: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=60)
    angle: str = Field(min_length=1, max_length=400)
    audience_value: str = Field(min_length=1, max_length=400)
    evidence: str = Field(min_length=1, max_length=800)


class TopicBatch(StrictModel):
    topics: list[Topic] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({t.id for t in self.topics}) != len(self.topics):
            raise ValueError("选题 ID 不可重复")
        import re

        normalized = [re.sub(r"\W+", "", t.title.casefold()) for t in self.topics]
        if len(set(normalized)) != len(normalized):
            raise ValueError("候选选题标题不可重复")
        return self


class Citation(StrictModel):
    source_id: str = Field(min_length=1, max_length=100)
    claim: str = Field(min_length=1, max_length=300)


class Article(StrictModel):
    title: str = Field(min_length=1, max_length=60)
    digest: str = Field(min_length=1, max_length=120)
    markdown: str = Field(min_length=200, max_length=15000)
    fact_checks: list[str] = Field(default_factory=list, max_length=15)
    citations: list[Citation] = Field(default_factory=list, max_length=20)


class Slide(StrictModel):
    title: str = Field(min_length=1, max_length=26)
    subtitle: str = Field("", max_length=60)
    bullets: list[str] = Field(min_length=1, max_length=4)
    code: str = Field("", max_length=420)
    takeaway: str = Field("", max_length=60)

    @field_validator("bullets")
    @classmethod
    def short_bullets(cls, value):
        if any(not item.strip() or len(item) > 85 for item in value):
            raise ValueError("每个要点应有 1–85 个字符")
        return value

    @field_validator("code")
    @classmethod
    def short_code(cls, value):
        if len(value.splitlines()) > 8 or any(len(line) > 48 for line in value.splitlines()):
            raise ValueError("代码最多 8 行，每行最多 48 字符")
        return value


class CardPack(StrictModel):
    title: str = Field(min_length=1, max_length=20)
    caption: str = Field(min_length=1, max_length=800)
    tags: list[str] = Field(min_length=1, max_length=8)
    slides: list[Slide] = Field(min_length=3, max_length=5)

    @field_validator("tags")
    @classmethod
    def short_tags(cls, value):
        if any(not t or len(t) > 20 for t in value):
            raise ValueError("话题标签应有 1–20 个字符")
        return value


class SelectTopic(StrictModel):
    interrupt_id: str = Field(min_length=1, max_length=128)
    topic_id: str = Field(min_length=1, max_length=60)
    operator: str = Field(min_length=1, max_length=80)


class Review(StrictModel):
    interrupt_id: str = Field(min_length=1, max_length=128)
    article_version: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    feedback: str = Field("", max_length=3000)
    edited_article: Article | None = None
    operator: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def require_feedback(self):
        if self.decision == "rejected" and not self.feedback:
            raise ValueError("打回重写必须填写修改意见")
        if self.decision == "rejected" and self.edited_article:
            raise ValueError("打回时只提交修改意见；手工定稿请使用 approved")
        return self


class TopicReview(StrictModel):
    interrupt_id: str = Field(min_length=1, max_length=128)
    topic_round: int = Field(0, ge=0)
    decision: Literal["approved", "rejected"]
    feedback: str = Field("", max_length=4000)
    operator: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def feedback_required(self):
        if self.decision == "rejected" and not self.feedback.strip():
            raise ValueError("打回选题需要填写原因")
        return self


class KnowledgeCreate(StrictModel):
    category: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    source_url: HttpUrl | None = None


class FeedbackCreate(StrictModel):
    platform: Literal["xiaohongshu", "wechat"]
    views: int = Field(ge=0, le=2147483647)
    likes: int = Field(ge=0, le=2147483647)
    saves: int = Field(ge=0, le=2147483647)
    comments: int = Field(ge=0, le=2147483647)
    observed_at: datetime
    note: str = Field("", max_length=2000)

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("observed_at 必须带时区，例如 +08:00")
        return value
