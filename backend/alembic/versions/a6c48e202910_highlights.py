"""Local workflow optimization, evaluations and authentication.

Revision ID: a6c48e202910
"""

import sqlalchemy as sa
from alembic import op

revision = "a6c48e202910"
down_revision = "fb72788fb46f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "media_users", sa.Column("role", sa.String(20), nullable=False, server_default="editor")
    )
    op.add_column(
        "media_users", sa.Column("disabled", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column(
        "media_users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "media_call_metrics", sa.Column("modality", sa.String(20), nullable=False, server_default="text")
    )
    from sqlalchemy.dialects.postgresql import JSONB

    op.add_column(
        "media_call_metrics", sa.Column("details", JSONB(), nullable=False, server_default="{}")
    )
    # Frozen DDL; this revision never imports future ORM definitions.
    op.execute(
        "CREATE TABLE media_prompt_versions (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(36) NOT NULL, \n\tstage VARCHAR(30) NOT NULL, \n\tname VARCHAR(100) NOT NULL, \n\tinstruction TEXT NOT NULL, \n\toptions JSONB NOT NULL, \n\tdigest VARCHAR(64) NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(owner_id) REFERENCES media_users (id)\n)"
    )
    op.execute("CREATE INDEX ix_media_prompt_versions_owner_id ON media_prompt_versions (owner_id)")
    op.execute(
        "CREATE TABLE media_prompt_experiments (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(36) NOT NULL, \n\tstage VARCHAR(30) NOT NULL, \n\tcontrol_id VARCHAR(36) NOT NULL, \n\ttreatment_id VARCHAR(36) NOT NULL, \n\tpercentage INTEGER NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (owner_id, stage), \n\tFOREIGN KEY(owner_id) REFERENCES media_users (id), \n\tFOREIGN KEY(control_id) REFERENCES media_prompt_versions (id), \n\tFOREIGN KEY(treatment_id) REFERENCES media_prompt_versions (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_media_prompt_experiments_owner_id ON media_prompt_experiments (owner_id)"
    )
    op.execute(
        "CREATE TABLE media_generation_ratings (\n\ttask_id VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(36) NOT NULL, \n\tscore INTEGER NOT NULL, \n\tdimensions JSONB NOT NULL, \n\tcomment TEXT NOT NULL, \n\tprompt_snapshot JSONB NOT NULL, \n\tresult_snapshot JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (task_id), \n\tFOREIGN KEY(task_id) REFERENCES media_tasks (id), \n\tFOREIGN KEY(owner_id) REFERENCES media_users (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_media_generation_ratings_owner_id ON media_generation_ratings (owner_id)"
    )
    op.execute(
        "CREATE TABLE media_evaluation_cases (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(36) NOT NULL, \n\ttask_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(150) NOT NULL, \n\tcontext JSONB NOT NULL, \n\texpected JSONB NOT NULL, \n\trules JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(owner_id) REFERENCES media_users (id), \n\tFOREIGN KEY(task_id) REFERENCES media_tasks (id)\n)"
    )
    op.execute("CREATE INDEX ix_media_evaluation_cases_owner_id ON media_evaluation_cases (owner_id)")
    op.execute(
        "CREATE TABLE media_evaluation_runs (\n\tid VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(36) NOT NULL, \n\trequest_key VARCHAR(128) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tcases JSONB NOT NULL, \n\tvariants JSONB NOT NULL, \n\tresults JSONB NOT NULL, \n\terror TEXT, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(owner_id) REFERENCES media_users (id), \n\tUNIQUE (request_key)\n)"
    )
    op.execute("CREATE INDEX ix_media_evaluation_runs_owner_id ON media_evaluation_runs (owner_id)")
    op.execute(
        "CREATE TABLE media_knowledge_chunks (\n\tid VARCHAR(36) NOT NULL, \n\tknowledge_id VARCHAR(36) NOT NULL, \n\towner_id VARCHAR(36), \n\tordinal INTEGER NOT NULL, \n\tbody TEXT NOT NULL, \n\tembedding JSONB, \n\tembedding_model VARCHAR(200), \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(knowledge_id) REFERENCES media_knowledge (id) ON DELETE CASCADE, \n\tFOREIGN KEY(owner_id) REFERENCES media_users (id)\n)"
    )
    op.execute(
        "CREATE INDEX ix_media_knowledge_chunks_knowledge_id ON media_knowledge_chunks (knowledge_id)"
    )
    op.execute("CREATE INDEX ix_media_knowledge_chunks_owner_id ON media_knowledge_chunks (owner_id)")
    op.execute(
        'CREATE TABLE media_rate_buckets (\n\tkey VARCHAR(64) NOT NULL, \n\t"window" INTEGER NOT NULL, \n\tcount INTEGER NOT NULL, \n\tPRIMARY KEY (key, "window")\n)'
    )
    op.execute(
        "CREATE TABLE media_safety_audits (\n\tid VARCHAR(36) NOT NULL, \n\ttask_id VARCHAR(36), \n\tstage VARCHAR(50) NOT NULL, \n\tverdict VARCHAR(30) NOT NULL, \n\tdetail JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(task_id) REFERENCES media_tasks (id)\n)"
    )
    op.execute("CREATE INDEX ix_media_safety_audits_task_id ON media_safety_audits (task_id)")
    op.execute(
        "CREATE TABLE media_oauth_attempts (\n\tstate_hash VARCHAR(64) NOT NULL, \n\tnonce VARCHAR(100) NOT NULL, \n\tverifier VARCHAR(150) NOT NULL, \n\tbrowser_hash VARCHAR(64) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (state_hash)\n)"
    )
    op.execute(
        "CREATE TABLE media_external_identities (\n\tid VARCHAR(36) NOT NULL, \n\tissuer VARCHAR(500) NOT NULL, \n\tsubject VARCHAR(300) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (issuer, subject), \n\tFOREIGN KEY(user_id) REFERENCES media_users (id)\n)"
    )


def downgrade():
    for name in (
        "media_external_identities",
        "media_oauth_attempts",
        "media_safety_audits",
        "media_rate_buckets",
        "media_knowledge_chunks",
        "media_evaluation_runs",
        "media_evaluation_cases",
        "media_generation_ratings",
        "media_prompt_experiments",
        "media_prompt_versions",
    ):
        op.drop_table(name)
    op.drop_column("media_call_metrics", "details")
    op.drop_column("media_call_metrics", "modality")
    for name in ("token_version", "disabled", "role"):
        op.drop_column("media_users", name)
