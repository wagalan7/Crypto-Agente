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
    media_url = Column(Text)  # public URL of image/video to publish
    status = Column(String(50), default="pending")
    scheduled_at = Column(DateTime)
    published_at = Column(DateTime)
    trend_context = Column(Text)
    strategic_note = Column(Text)
    external_post_id = Column(String(200))  # ID returned by Meta after publish
    publish_error = Column(Text)
    # Strategic reasoning (item 10: justificativa das decisões)
    objective_reasoning = Column(Text)  # why this objective
    emotion_used = Column(String(100))  # dominant emotion (e.g., "vulnerabilidade", "esperança")
    funnel_stage = Column(String(50))   # identificação / dor / autoridade / quebra_objecao / desejo / conversao
    format_reasoning = Column(Text)     # why this format
    linked_product_id = Column(Integer, ForeignKey("products.id"), nullable=True)  # product this content sells
    production_brief = Column(Text)  # JSON: shooting checklist (auto-generated on approve)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client", back_populates="contents")
    metrics = relationship("MetricsSnapshot", back_populates="content")
    linked_product = relationship("Product", foreign_keys=[linked_product_id])


class SocialAccount(Base):
    """Manually-entered tokens to publish on Instagram / Facebook for a client."""
    __tablename__ = "social_accounts"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    platform = Column(String(50), nullable=False)  # "instagram" or "facebook"
    account_id = Column(String(200), nullable=False)  # IG Business User ID or FB Page ID
    account_name = Column(String(200))  # display name (e.g., "@thiago.fitness")
    access_token = Column(Text, nullable=False)  # long-lived Page Access Token
    expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    last_error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client")


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


# ============================================================
# Strategic Intelligence Modules
# ============================================================

class Persona(Base):
    """Audience persona generated from bio, content, comments, metrics, language."""
    __tablename__ = "personas"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False, unique=True)
    pains = Column(JSON, default=list)              # ["medo de fracasso", ...]
    desires = Column(JSON, default=list)
    emotions = Column(JSON, default=list)           # dominant emotions
    insecurities = Column(JSON, default=list)
    audience_goals = Column(JSON, default=list)
    language_patterns = Column(Text)                # how they speak (long text)
    psychological_patterns = Column(Text)
    audience_profile = Column(Text)                 # demographic + psychographic
    evidence = Column(Text)                         # what data was used
    generated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = relationship("Client")


class Inspiration(Base):
    """Saved reference (URL, screenshot, text) with structured analysis."""
    __tablename__ = "inspirations"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    source_type = Column(String(50))    # "url" / "text" / "image"
    source_value = Column(Text)         # the URL or pasted text or image URL
    label = Column(String(300))         # user-given name
    analysis = Column(JSON, default=dict)  # {hook, narrative, cta, rhythm, retention, emotion, structure, visual_style}
    adapted_brief = Column(Text)        # how to adapt to this client's brand
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client")


class Insight(Base):
    """Strategic insight surfaced by the AI for the user (dashboard cards)."""
    __tablename__ = "insights"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    kind = Column(String(50))           # positioning / retention / format / audience / growth / authority / risk
    title = Column(String(300))
    message = Column(Text)
    evidence = Column(Text)
    severity = Column(String(20), default="info")  # info / warning / critical / opportunity
    is_dismissed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client")


class Product(Base):
    """Offer / product / service to monetize. Drives sales-aware content."""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    name = Column(String(300), nullable=False)
    type = Column(String(50))           # service / mentorship / course / ebook / offer
    price = Column(String(100))
    description = Column(Text)
    pains_solved = Column(JSON, default=list)
    desires = Column(JSON, default=list)
    objections = Column(JSON, default=list)
    transformation = Column(Text)
    awareness_stage = Column(String(50))  # unaware / problem / solution / product / most_aware
    funnel_stage = Column(String(50))     # top / middle / bottom
    is_primary = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client")


class KnowledgeItem(Base):
    """User's intellectual capital: notes, PDFs, books, ideas. AI absorbs vision."""
    __tablename__ = "knowledge_items"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    title = Column(String(300), nullable=False)
    content = Column(Text)              # extracted text
    source_type = Column(String(50))    # pdf / note / screenshot / idea / book / concept / reference
    tags = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client")


class WeeklyBrain(Base):
    """Weekly strategic summary: focus, opportunities, alerts, priorities."""
    __tablename__ = "weekly_brains"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    focus = Column(Text)                # main focus of the week
    opportunities = Column(JSON, default=list)
    alerts = Column(JSON, default=list)
    risks = Column(JSON, default=list)
    priorities = Column(JSON, default=list)
    audience_behavior = Column(Text)
    trends = Column(JSON, default=list)
    emotional_sequence = Column(JSON, default=list)  # day-by-day emotional plan
    generated_at = Column(DateTime, default=datetime.utcnow)

    client = relationship("Client")
