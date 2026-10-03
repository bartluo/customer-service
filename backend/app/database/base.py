"""ORM 基类。后续所有表模型都继承 Base。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全局 ORM 基类。"""
