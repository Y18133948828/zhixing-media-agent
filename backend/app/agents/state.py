from typing import TypedDict


class MediaState(TypedDict, total=False):
    task_id: str
    task_input: dict
    context: dict
    mode: str
    candidate_topics: list[dict]
    selected_topic: dict
    article_draft: dict
    article_version: int
    revision_count: int
    review_status: str
    human_feedback: str
    revisions: list[dict]
    review_history: list[dict]
    selection_record: dict
    approved_article: dict
    xhs_pack: dict
    xhs_files: list[dict]
    wechat_files: list[dict]
    wechat_html_content: str
    ready: bool
    last_render_job_id: str
    review_index: int
    review_article_candidate: dict
    topic_round: int
    topic_review_index: int
    topic_review_status: str
    topic_feedback: str
    topic_review_history: list[dict]


class PlanningOutput(TypedDict):
    candidate_topics: list[dict]
    selected_topic: dict
    topic_review_index: int
    topic_review_status: str


class WritingOutput(TypedDict):
    article_draft: dict
    article_version: int
    revision_count: int
    review_status: str
    revisions: list[dict]
    review_index: int
    review_article_candidate: dict
    approved_article: dict


class VisualOutput(TypedDict):
    xhs_pack: dict
    xhs_files: list[dict]
