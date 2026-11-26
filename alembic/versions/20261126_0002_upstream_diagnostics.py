"""작업과 대사에 외부 서비스 오류를 기록한다."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("jobs", sa.Column("warnings", JSONB(), nullable=True))
    op.add_column("dialogue_lines", sa.Column("audio_error", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("dialogue_lines", "audio_error")
    op.drop_column("jobs", "warnings")
