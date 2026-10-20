"""give imported contacts a trade from the column they were named in

Revision ID: b3c1d2e4f5a6
Revises: 700475bf60cb
Create Date: 2026-10-20 18:00:00

"""

from typing import Sequence, Union

from alembic import op

revision: str = "b3c1d2e4f5a6"
down_revision: Union[str, Sequence[str], None] = "700475bf60cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Most specific first: someone named as contractor on one site and contact person on another
# is a contractor. Matches contacts.ROLE_TRADES.
ORDER = [
    ("contractor", "contractor"),
    ("architect", "architect"),
    ("owner", "owner"),
    ("company", "company"),
    ("contact_person", "site_contact"),
]


def upgrade() -> None:
    for role, trade in ORDER:
        op.execute(
            f"UPDATE contacts SET trade = '{trade}' WHERE trade IS NULL AND id IN "
            f"(SELECT contact_id FROM project_contacts WHERE role = '{role}')"
        )


def downgrade() -> None:
    trades = ", ".join(f"'{t}'" for _, t in ORDER)
    op.execute(
        f"UPDATE contacts SET trade = NULL WHERE trade IN ({trades}) AND id NOT IN "
        "(SELECT contact_id FROM contact_notes)"
    )
