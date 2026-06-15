"""withdraw: create withdraw_request (S79 D2/D6).

Revision ID: 20260612_1200_withdraw
Revises: vbwd_001
Create Date: 2026-06-12

Own branch anchored on the always-present core root (`vbwd_001`) — never
on another plugin's revision (migration-graph fragmentation trap).
`vbwd_user` exists in the root, so the ON DELETE CASCADE FK resolves
standalone. The `WITHDRAW` enum value the plugin writes through core
`TokenService` is added by the CORE migration
`20260612_1100_withdraw_tx` (D5).
"""
from alembic import op
import sqlalchemy as sa


revision = "20260612_1200_withdraw"
down_revision = "vbwd_001"
branch_labels = None
depends_on = None

TABLE_NAME = "withdraw_request"


def upgrade():
    conn = op.get_bind()
    if _table_exists(conn, TABLE_NAME):
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "balance_source",
            sa.String(50),
            nullable=False,
            server_default="tokens",
        ),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("payout_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column(
            "destination", sa.dialects.postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column("provider_payout_id", sa.String(255), nullable=True),
        sa.Column("error", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["vbwd_user.id"],
            name="fk_withdraw_request_user_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_withdraw_request_user_id", TABLE_NAME, ["user_id"])
    op.create_index("ix_withdraw_request_status", TABLE_NAME, ["status"])


def downgrade():
    op.drop_index("ix_withdraw_request_status", table_name=TABLE_NAME)
    op.drop_index("ix_withdraw_request_user_id", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_name = :name"),
        {"name": table_name},
    )
    return result.scalar() is not None
