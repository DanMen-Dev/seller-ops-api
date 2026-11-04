"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-04 10:24:40.389139
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('tenants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('api_key_hash', sa.String(length=64), nullable=False),
    sa.Column('seller_id', sa.String(length=40), nullable=False),
    sa.Column('ads_profile_id', sa.String(length=40), nullable=False),
    sa.Column('refresh_token_enc', sa.Text(), nullable=False),
    sa.Column('marketplaces', sa.ARRAY(sa.String(length=2)), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('api_key_hash')
    )
    op.create_table('bid_changes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('keyword_id', sa.String(length=40), nullable=False),
    sa.Column('keyword_text', sa.String(length=200), nullable=False),
    sa.Column('rule', sa.String(length=80), nullable=False),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('old_bid', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('new_bid', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('applied_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bid_changes_tenant_id'), 'bid_changes', ['tenant_id'], unique=False)
    op.create_table('daily_sales',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('marketplace', sa.String(length=2), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('units', sa.Integer(), nullable=False),
    sa.Column('revenue', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'sku', 'marketplace', 'day')
    )
    op.create_index('ix_daily_sales_tenant_day', 'daily_sales', ['tenant_id', 'day'], unique=False)
    op.create_table('inventory',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('fulfillable', sa.Integer(), nullable=False),
    sa.Column('inbound', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'sku')
    )
    op.create_table('keyword_stats',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('campaign_id', sa.String(length=40), nullable=False),
    sa.Column('keyword_id', sa.String(length=40), nullable=False),
    sa.Column('keyword_text', sa.String(length=200), nullable=False),
    sa.Column('match_type', sa.String(length=10), nullable=False),
    sa.Column('bid', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('impressions', sa.Integer(), nullable=False),
    sa.Column('clicks', sa.Integer(), nullable=False),
    sa.Column('spend', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('sales', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('orders', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'keyword_id', 'day')
    )
    op.create_index('ix_keyword_stats_tenant_day', 'keyword_stats', ['tenant_id', 'day'], unique=False)
    op.create_table('offers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('marketplace', sa.String(length=2), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('referral_fee', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('fba_fee', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'sku', 'marketplace')
    )
    op.create_table('products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('asin', sa.String(length=10), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('unit_cost', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('cost_currency', sa.String(length=3), nullable=False),
    sa.Column('lead_time_days', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'sku')
    )
    op.create_table('sync_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sync_runs_tenant_id'), 'sync_runs', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sync_runs_tenant_id'), table_name='sync_runs')
    op.drop_table('sync_runs')
    op.drop_table('products')
    op.drop_table('offers')
    op.drop_index('ix_keyword_stats_tenant_day', table_name='keyword_stats')
    op.drop_table('keyword_stats')
    op.drop_table('inventory')
    op.drop_index('ix_daily_sales_tenant_day', table_name='daily_sales')
    op.drop_table('daily_sales')
    op.drop_index(op.f('ix_bid_changes_tenant_id'), table_name='bid_changes')
    op.drop_table('bid_changes')
    op.drop_table('tenants')
