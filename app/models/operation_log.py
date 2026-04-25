from sqlalchemy import String, Text, BigInteger, Integer
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base, TimestampMixin
import uuid

class OperationLog(Base, TimestampMixin):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    resource: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(100), nullable=True)
    request_method: Mapped[str] = mapped_column(String(10))
    request_path: Mapped[str] = mapped_column(String(500))
    request_ip: Mapped[str] = mapped_column(String(45))
    response_status: Mapped[int] = mapped_column(Integer)
    execution_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=True)