"""raw payload references

Revision ID: 20260509_0002
Revises: 20260509_0001
Create Date: 2026-05-09 00:02:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260509_0002"
down_revision = "20260509_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signal_snapshots", sa.Column("raw_payload_uri", sa.Text(), nullable=True))
    op.add_column("signal_snapshots", sa.Column("raw_payload_sha256", sa.String(length=64), nullable=True))
    op.add_column("signal_snapshots", sa.Column("raw_payload_bytes", sa.Integer(), nullable=True))
    op.add_column("signal_snapshots", sa.Column("raw_payload_compression", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("signal_snapshots", "raw_payload_compression")
    op.drop_column("signal_snapshots", "raw_payload_bytes")
    op.drop_column("signal_snapshots", "raw_payload_sha256")
    op.drop_column("signal_snapshots", "raw_payload_uri")
