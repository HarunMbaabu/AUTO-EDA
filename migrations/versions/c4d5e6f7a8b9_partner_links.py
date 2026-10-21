"""partner links (website, WhatsApp catalogue)

Revision ID: c4d5e6f7a8b9
Revises: 79ff4ea2c358
Create Date: 2026-10-21 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "79ff4ea2c358"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "contacts",
        sa.Column(
            "links", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("contacts", "links")
