"""initial schema — all Phase-1.5 tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-13
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from bruce_engine.schema import Base

    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    from bruce_engine.schema import Base

    Base.metadata.drop_all(op.get_bind())
