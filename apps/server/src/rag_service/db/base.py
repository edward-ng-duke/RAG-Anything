"""SQLAlchemy 2.0 declarative base for the rag_service ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


metadata = Base.metadata
