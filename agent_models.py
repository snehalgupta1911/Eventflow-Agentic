from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Text
from datetime import datetime
from database import Base

class CommunicationLog(Base):
    __tablename__ = "communication_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    recipient_email = Column(String, index=True, nullable=False)
    recipient_role = Column(String, nullable=False) # 'participant', 'judge', 'mentor'
    subject = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    stage = Column(String, nullable=False) # 'welcome', 'evaluation_reminder', 'results_notification', 'progression_invitation'
    status = Column(String, default="draft") # 'draft', 'sent', 'failed'
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)

class ScoreAnomalyExplanation(Base):
    __tablename__ = "score_anomaly_explanations"

    id = Column(Integer, primary_key=True, index=True)
    anomaly_id = Column(Integer, ForeignKey("score_anomalies.id"), unique=True)
    explanation = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
