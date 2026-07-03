"""Add operator_filter to call_result_import_rows

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-03 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "call_result_import_rows",
        sa.Column("operator_filter", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_call_result_import_rows_operator_filter",
        "call_result_import_rows",
        ["operator_filter"],
    )


def downgrade() -> None:
    op.drop_index("ix_call_result_import_rows_operator_filter", table_name="call_result_import_rows")
    op.drop_column("call_result_import_rows", "operator_filter")
