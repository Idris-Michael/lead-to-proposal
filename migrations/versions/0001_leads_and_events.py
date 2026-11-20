"""leads and lead_events

Revision ID: 0001
Revises:
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column("email", sa.String(320), nullable=False, index=True),
        sa.Column("company", sa.String(160), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("qualification", sa.JSON),
        sa.Column("hubspot_contact_id", sa.String(32)),
        sa.Column("hubspot_deal_id", sa.String(32)),
        sa.Column("hubspot_note_id", sa.String(32)),
        sa.Column("proposal_md", sa.Text),
        sa.Column("price_issues", sa.JSON),
        sa.Column("approval_ref", sa.String(64)),
        sa.Column("error", sa.Text),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("drafted_at", sa.DateTime(timezone=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The worker's claim query: waiting rows in arrival order.
    op.create_index("ix_leads_status_received", "leads", ["status", "received_at"])
    op.create_table(
        "lead_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("lead_id", sa.String(36), sa.ForeignKey("leads.id"), nullable=False, index=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("detail", sa.JSON, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("lead_events")
    op.drop_index("ix_leads_status_received", table_name="leads")
    op.drop_table("leads")
