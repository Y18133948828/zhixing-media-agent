"""workbench_users_and_metrics"""

import sqlalchemy as sa
from alembic import op

revision = "fb72788fb46f"
down_revision = "e1e23761b717"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("password_hash", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "media_call_metrics",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("usage_source", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["media_tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_media_call_metrics_task_id"), "media_call_metrics", ["task_id"], unique=False
    )
    op.add_column("media_knowledge", sa.Column("owner_id", sa.String(length=36), nullable=True))
    op.create_index(op.f("ix_media_knowledge_owner_id"), "media_knowledge", ["owner_id"], unique=False)
    op.create_foreign_key(None, "media_knowledge", "media_users", ["owner_id"], ["id"])
    op.add_column("media_tasks", sa.Column("owner_id", sa.String(length=36), nullable=True))
    op.add_column(
        "media_tasks", sa.Column("title", sa.String(length=200), server_default="", nullable=False)
    )
    op.add_column(
        "media_tasks", sa.Column("archived", sa.Boolean(), server_default="false", nullable=False)
    )
    op.create_index(op.f("ix_media_tasks_owner_id"), "media_tasks", ["owner_id"], unique=False)
    op.create_foreign_key(None, "media_tasks", "media_users", ["owner_id"], ["id"])


def downgrade():
    op.drop_constraint("media_tasks_owner_id_fkey", "media_tasks", type_="foreignkey")
    op.drop_index(op.f("ix_media_tasks_owner_id"), table_name="media_tasks")
    op.drop_column("media_tasks", "archived")
    op.drop_column("media_tasks", "title")
    op.drop_column("media_tasks", "owner_id")
    op.drop_constraint("media_knowledge_owner_id_fkey", "media_knowledge", type_="foreignkey")
    op.drop_index(op.f("ix_media_knowledge_owner_id"), table_name="media_knowledge")
    op.drop_column("media_knowledge", "owner_id")
    op.drop_index(op.f("ix_media_call_metrics_task_id"), table_name="media_call_metrics")
    op.drop_table("media_call_metrics")
    op.drop_table("media_users")
