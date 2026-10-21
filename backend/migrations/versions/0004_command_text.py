"""command text: redacted (always) and raw (opt-in) attacker input

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('execution_request', schema=None) as batch_op:
        batch_op.add_column(sa.Column('input_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('input_raw', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('execution_request', schema=None) as batch_op:
        batch_op.drop_column('input_raw')
        batch_op.drop_column('input_text')
