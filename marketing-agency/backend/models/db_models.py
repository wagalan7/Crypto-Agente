from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from database import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
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

    contents = relationship("ContentPiece", back_populates="client")
    calendar_slots = relationship("CalendarSlot", back_populates="client")
    metrics = relationship("MetricsSnapshot", back_populates="client")
    memories = relationship("AgentMemory", back_populates="client")


class ContentPiece(Base):
    __tablename__ = "content_pieces"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    title = Column(String(500))
    format = Column(String(50))  # reels, carousel, story, post, short, youtube
    platform = Column(String(50))
    objective = Column(String(100))  # attract, connect, position, sell, break_objection, authority
    hook = Column(Text)
    script = Column(Text)
    copy = Column(Text)
    design_brief = Column(Text)
    status = Column(String(50), default="pending")  # pending, approved, recorded, published
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
    status = Column(String(50), default="planned")  # planned, ready, published

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
    agent_type = Column(String(50))  # strategy, analytics, script, trend, design, amplifier
    memory_key = Column(String(200))
    memory_value = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="memories")
