"""Local web-operator account model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WebUser(Base):
    """Local authentication data without profile or personal information."""

    __tablename__ = "web_users"

    uuid: Mapped[UUID] = mapped_column(Uuid(native_uuid=True), primary_key=True)
    normalized_username: Mapped[str] = mapped_column(
        String(64, collation="C"),
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )
    session_version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    failed_login_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "normalized_username",
            name="uq_web_users_normalized_username",
        ),
        CheckConstraint(
            "normalized_username ~ '^[a-z0-9][a-z0-9._-]{2,63}$'",
            name="normalized_username",
        ),
        CheckConstraint(
            "length(password_hash) > 0 AND password_hash LIKE '$argon2id$%'",
            name="password_hash_argon2id",
        ),
        CheckConstraint(
            "session_version >= 0",
            name="session_version_non_negative",
        ),
        CheckConstraint(
            "failed_login_count >= 0",
            name="failed_login_count_non_negative",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="updated_at",
        ),
    )


__all__ = ["WebUser"]
