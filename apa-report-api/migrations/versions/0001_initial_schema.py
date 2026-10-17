"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-17 16:30:01.333669
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('reports',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.String(length=255), nullable=False),
    sa.Column('topic', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('progress_percent', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('checkpoint_stage', sa.String(length=32), nullable=True),
    sa.Column('lease_owner', sa.String(length=128), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('abstract', sa.Text(), nullable=True),
    sa.Column('keywords', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('methodology', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('token_usage', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('content_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('QUEUED', 'PLANNING', 'AWAITING_APPROVAL', 'QUEUED_FOR_GENERATION', 'SEARCHING', 'SCREENING', 'EXTRACTING', 'WRITING', 'VERIFYING', 'COMPLETED', 'FAILED', 'CANCELLED')", name=op.f('ck_reports_status_valid')),
    sa.CheckConstraint('progress_percent BETWEEN 0 AND 100', name=op.f('ck_reports_progress_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports'))
    )
    op.create_index('ix_reports_user_created', 'reports', ['user_id', 'created_at', 'id'], unique=False)
    op.create_index('ix_reports_user_status', 'reports', ['user_id', 'status'], unique=False)
    op.create_table('evidence_cards',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('sub_question_id', sa.String(length=64), nullable=False),
    sa.Column('work_id', sa.String(length=32), nullable=False),
    sa.Column('finding', sa.Text(), nullable=False),
    sa.Column('design', sa.Text(), nullable=True),
    sa.Column('sample', sa.Text(), nullable=True),
    sa.Column('effect_direction', sa.String(length=32), nullable=True),
    sa.Column('claim_strength', sa.String(length=32), nullable=True),
    sa.Column('limitations', sa.Text(), nullable=True),
    sa.Column('background_only', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_evidence_cards_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_evidence_cards')),
    sa.UniqueConstraint('report_id', 'sub_question_id', 'work_id', name='uq_evidence_report_sq_work')
    )
    op.create_index(op.f('ix_evidence_cards_report_id'), 'evidence_cards', ['report_id'], unique=False)
    op.create_table('outbox',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('queue', sa.String(length=32), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_outbox_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox'))
    )
    op.create_index(op.f('ix_outbox_report_id'), 'outbox', ['report_id'], unique=False)
    op.create_index('ix_outbox_unsent', 'outbox', ['created_at'], unique=False, postgresql_where='sent_at IS NULL')
    op.create_table('papers',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('sub_question_id', sa.String(length=64), nullable=False),
    sa.Column('work_id', sa.String(length=32), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('seed_connections', sa.Integer(), nullable=False),
    sa.Column('sources', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('work', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_papers_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_papers')),
    sa.UniqueConstraint('report_id', 'sub_question_id', 'work_id', name='uq_papers_report_sq_work')
    )
    op.create_index(op.f('ix_papers_report_id'), 'papers', ['report_id'], unique=False)
    op.create_table('plans',
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('central_question', sa.Text(), nullable=False),
    sa.Column('interpretation_note', sa.Text(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('locked', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_plans_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('report_id', name=op.f('pk_plans'))
    )
    op.create_table('report_references',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('work_id', sa.String(length=32), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('apa_citation', sa.Text(), nullable=False),
    sa.Column('in_text_citation', sa.Text(), nullable=False),
    sa.Column('authors', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=True),
    sa.Column('volume', sa.String(length=64), nullable=True),
    sa.Column('issue', sa.String(length=64), nullable=True),
    sa.Column('pages', sa.String(length=64), nullable=True),
    sa.Column('doi', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('is_open_access', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_report_references_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_references')),
    sa.UniqueConstraint('report_id', 'work_id', name='uq_report_references_report_work')
    )
    op.create_index(op.f('ix_report_references_report_id'), 'report_references', ['report_id'], unique=False)
    op.create_table('report_sections',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('heading', sa.Text(), nullable=False),
    sa.Column('sub_question_id', sa.String(length=64), nullable=True),
    sa.Column('draft_content', sa.Text(), nullable=False),
    sa.Column('verified_content', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_report_sections_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_sections')),
    sa.UniqueConstraint('report_id', 'order', name='uq_report_sections_report_order')
    )
    op.create_index(op.f('ix_report_sections_report_id'), 'report_sections', ['report_id'], unique=False)
    op.create_table('screening_results',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('sub_question_id', sa.String(length=64), nullable=False),
    sa.Column('work_id', sa.String(length=32), nullable=False),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('included', sa.Boolean(), nullable=False),
    sa.CheckConstraint('score BETWEEN 0 AND 3', name=op.f('ck_screening_results_score_range')),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_screening_results_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_screening_results')),
    sa.UniqueConstraint('report_id', 'sub_question_id', 'work_id', name='uq_screening_report_sq_work')
    )
    op.create_index(op.f('ix_screening_results_report_id'), 'screening_results', ['report_id'], unique=False)
    op.create_table('sub_questions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('report_id', sa.Uuid(), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.CheckConstraint("role IN ('BACKGROUND', 'CORE', 'DISCUSSION')", name=op.f('ck_sub_questions_role_valid')),
    sa.CheckConstraint('"order" >= 1', name=op.f('ck_sub_questions_order_positive')),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_sub_questions_report_id_reports'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sub_questions'))
    )
    op.create_index(op.f('ix_sub_questions_report_id'), 'sub_questions', ['report_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sub_questions_report_id'), table_name='sub_questions')
    op.drop_table('sub_questions')
    op.drop_index(op.f('ix_screening_results_report_id'), table_name='screening_results')
    op.drop_table('screening_results')
    op.drop_index(op.f('ix_report_sections_report_id'), table_name='report_sections')
    op.drop_table('report_sections')
    op.drop_index(op.f('ix_report_references_report_id'), table_name='report_references')
    op.drop_table('report_references')
    op.drop_table('plans')
    op.drop_index(op.f('ix_papers_report_id'), table_name='papers')
    op.drop_table('papers')
    op.drop_index('ix_outbox_unsent', table_name='outbox', postgresql_where='sent_at IS NULL')
    op.drop_index(op.f('ix_outbox_report_id'), table_name='outbox')
    op.drop_table('outbox')
    op.drop_index(op.f('ix_evidence_cards_report_id'), table_name='evidence_cards')
    op.drop_table('evidence_cards')
    op.drop_index('ix_reports_user_status', table_name='reports')
    op.drop_index('ix_reports_user_created', table_name='reports')
    op.drop_table('reports')
