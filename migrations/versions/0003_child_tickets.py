"""child tickets: ticket.parent_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-23 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ticket', sa.Column('parent_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_ticket_parent_id_ticket', 'ticket', 'ticket', ['parent_id'], ['id'])
    op.create_index('ix_ticket_parent_id', 'ticket', ['parent_id'])


def downgrade():
    op.drop_index('ix_ticket_parent_id', table_name='ticket')
    op.drop_constraint('fk_ticket_parent_id_ticket', 'ticket', type_='foreignkey')
    op.drop_column('ticket', 'parent_id')
