"""outbound_messages.to_handle — the recipient the relay sends to

The Mac relay claims a durable outbound_messages row and sends it via imsg; it needs the recipient
handle / chat_guid. Conditional ALTER (create_all adds it on a fresh DB).

Revision ID: 0008_outbound_to_handle
Revises: 0007_relay_devices
Create Date: 2026-07-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_outbound_to_handle"
down_revision = "0007_relay_devices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "outbound_messages" in insp.get_table_names():
        have = {c["name"] for c in insp.get_columns("outbound_messages")}
        if "to_handle" not in have:
            op.add_column("outbound_messages", sa.Column("to_handle", sa.String(255), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE outbound_messages DROP COLUMN IF EXISTS to_handle")
