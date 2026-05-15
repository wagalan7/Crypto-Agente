from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, JSON, Boolean, Enum
from sqlalchemy.orm import relationship
from database import Base
import enum


class UserRole(str, enum.Enum):
    master = "master"
    admin = "admin"
    user = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    name = Column(String(200))
    role = Column(String(20), default="user")  # master / admin / user
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    clients = relationship("Client", back_populates="owner", foreign_keys="Client.owner_id")
    granted_access = relationship("ClientAccess", back_populates="user", foreign_keys="ClientAccess.user_id")


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(200), nullable=False)
    niche = Column(String(200))
    target_audience = Column(Text)
    tone = Column(String(100))
    personality = Column(Text)
    positioning = Column(Text)
    goals = Column(JSON, default=list)
    platforms = Column(JSON, default=list)
    authority_score = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="clients", foreign_keys=[owner_id])
    access_grants = relationship("ClientAccess", back_populates="client")
    contents = relationship("ContentPiece", back_populates="client")
    calendar_slots = relationship("CalendarSlot", back_populates="client")
    metrics = relationship("MetricsSnapshot", back_populates="client")
    memories = relationship("AgentMemory", back_populates="client")


class ClientAccess(Base):
    """Grants a non-owner user access to a client."""
    __tablename__ = "client_access"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    granted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="access_grants")
    user = relationship("User", back_populates="granted_access", foreign_keys=[user_id])


class ContentPiece(Base):
    __tablename__ = "content_pieces"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    title = Column(String(500))
    format = Column(String(50))
    platform = Column(String(50))
    objective = Column(String(100))
    hook = Column(Text)
    script = Column(Text)
    copy = Column(Text)
    design_brief = Column(Text)
    status = Column(String(50), default="pending")
    scheduled_at = Column(DateTime)
    published_at = Column(DateTime)
    trend_context = Column(Text)
    strategic_note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="contents")
    metrics = relationship("MetricsSnapshot", back_populates="content")


class CalendarSlot(Base):
    __tablename__ = "calendar_slots"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    content_id = Column(Integer, ForeignKey("content_pieces.id"), nullable=True)
    scheduled_at = Column(DateTime, nullable=False)
    platform = Column(String(50))
    format = Column(String(50))
    objective = Column(String(100))
    status = Column(String(50), default="planned")

    client = relationship("Client", back_populates="calendar_slots")
    content = relationship("ContentPiece")


class MetricsSnapshot(Base):
    __tablename__ = "metrics_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    content_id = Column(Integer, ForeignKey("content_pieces.id"), nullable=True)
    platform = Column(String(50))
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    saves = Column(Integer, default=0)
    reach = Column(Integer, default=0)
    retention_rate = Column(Float, default=0.0)
    ctr = Column(Float, default=0.0)
    conversion_rate = Column(Float, default=0.0)
    raw_data = Column(JSON, default=dict)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="metrics")
    content = relationship("ContentPiece", back_populates="metrics")


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    agent_type = Column(String(50))
    memory_key = Column(String(200))
    memory_value = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="memories")
