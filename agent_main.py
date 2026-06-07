import os
import sys
from datetime import datetime, timedelta
from typing import List
from fastapi import Depends, HTTPException, Body, Query, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

# ==========================================
# 1. SQLITE & JSONB COMPATIBILITY MONKEYPATCH
# ==========================================
import sqlalchemy
from sqlalchemy import JSON

original_create_engine = sqlalchemy.create_engine
def custom_create_engine(url, *args, **kwargs):
    if url and url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return original_create_engine(url, *args, **kwargs)
sqlalchemy.create_engine = custom_create_engine

# Fallback PostgreSQL JSONB to SQLite JSON column type
import sqlalchemy.dialects.postgresql as pg
pg.JSONB = JSON

# Set default local database if DATABASE_URL is not set
if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite:///./eventflow.db"

# ==========================================
# 2. IMPORT ORIGINAL APP & BASE MODELS
# ==========================================
from main import app, get_db
from database import engine, Base
import models
import agent_models
import agent_tasks
import agent
import schemas

# Create all tables (original and new agentic tables)
Base.metadata.create_all(bind=engine)

# ==========================================
# 3. INTERCEPT AND OVERRIDE ROUTE HANDLERS
# ==========================================

def remove_route_by_path(path: str, methods: List[str]):
    """Removes a registered route from the FastAPI application."""
    routes_to_keep = [
        r for r in app.routes 
        if not (getattr(r, "path", None) == path and set(getattr(r, "methods", None) or []) == set(methods))
    ]
    app.routes.clear()
    app.routes.extend(routes_to_keep)

# Override A: Team Approval (triggers welcome email drafts)
remove_route_by_path("/events/{event_id}/approve-teams/", ["POST"])
def run_draft_welcome_emails_background(team_id: int, event_id: int):
    from database import SessionLocal
    db = SessionLocal()
    try:
        import agent_tasks
        agent_tasks.draft_welcome_emails_task(team_id, event_id, db)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error in background welcome email drafting: {e}")
    finally:
        db.close()

@app.post("/events/{event_id}/approve-teams/")
def approve_teams_override(event_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    event_state_val = event.state.value if hasattr(event.state, 'value') else event.state
    if event_state_val not in ["teams_proposed", "TEAMS_PROPOSED", "active", "ACTIVE"]:
        raise HTTPException(status_code=400, detail=f"Teams are not currently pending approval. State is: {event_state_val}")

    pending_teams = db.query(models.Team).filter(
        models.Team.event_id == event_id,
        models.Team.is_approved == 0
    ).all()

    if not pending_teams:
        raise HTTPException(status_code=400, detail="No pending teams found.")

    team_ids = [team.id for team in pending_teams]

    # 1. Update database records instantly to release lock
    for team in pending_teams:
        team.is_approved = 1

    # Transition event stage if not already active
    if event_state_val in ["teams_proposed", "TEAMS_PROPOSED"]:
        try:
            event.state = models.EventState.ACTIVE
        except Exception:
            event.state = "ACTIVE"
        
    db.commit()

    # 2. Queue the slow LLM email generation tasks in the background
    for team_id in team_ids:
        background_tasks.add_task(run_draft_welcome_emails_background, team_id, event_id)

    return {
        "status": "success", 
        "message": f"{len(team_ids)} teams successfully approved. Welcome emails are being drafted in the background.",
        "new_event_state": event.state.value if hasattr(event.state, 'value') else event.state
    }

# Override C: Form Teams (to enforce schemas.TeamResponse serialization)
remove_route_by_path("/events/{event_id}/form-teams/", ["POST"])
@app.post("/events/{event_id}/form-teams/", response_model=List[schemas.TeamResponse])
def form_teams_override(event_id: int, db: Session = Depends(get_db)):
    try:
        # Clear existing unapproved teams and rationales to allow reforming teams with new rules
        existing_unapproved_teams = db.query(models.Team).filter(
            models.Team.event_id == event_id,
            models.Team.is_approved == 0
        ).all()
        for team in existing_unapproved_teams:
            for member in team.members:
                member.team_id = None
            db.delete(team)
            
        # Clean up related welcome email drafts
        db.query(agent_models.CommunicationLog).filter(
            agent_models.CommunicationLog.event_id == event_id,
            agent_models.CommunicationLog.stage == "welcome",
            agent_models.CommunicationLog.status == "draft"
        ).delete(synchronize_session=False)
        db.commit()

        teams = services.generate_teams_algorithmically(event_id, db)
        
        # Start rationale generation for each team in background
        for team in teams:
            agent_tasks.generate_team_rationale_task(team.id, event_id, db)

        # Convert to Pydantic models while the database session is still open
        return [schemas.TeamResponse.model_validate(team) for team in teams]
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

# Override B: Submit Evaluation (triggers score anomaly explanation)
remove_route_by_path("/events/{event_id}/evaluations/", ["POST"])
@app.post("/events/{event_id}/evaluations/")
def submit_evaluation_override(
    event_id: int, 
    team_id: int = Body(...),
    judge_id: str = Body(...),
    scores: dict = Body(...), 
    db: Session = Depends(get_db)
):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    config = event.configuration or {}
    weights = config.get("scoring_weights", {"technical": 1.0}) 
    anomaly_threshold = config.get("anomaly_threshold", 15.0)

    # Score Consolidation
    weighted_total = sum(scores.get(category, 0) * weight for category, weight in weights.items())

    # Save evaluation
    evaluation = models.Evaluation(
        event_id=event_id,
        team_id=team_id,
        judge_id=judge_id,
        scores=scores,
        weighted_total=weighted_total
    )
    db.add(evaluation)
    db.commit()

    # Anomaly Detection
    team_evals = db.query(models.Evaluation).filter(models.Evaluation.team_id == team_id).all()
    anomalies_flagged = []
    
    if len(team_evals) > 1:
        panel_average = sum(e.weighted_total for e in team_evals) / len(team_evals)

        for eval_record in team_evals:
            deviation = abs(eval_record.weighted_total - panel_average)

            if deviation > anomaly_threshold:
                existing_anomaly = db.query(models.ScoreAnomaly).filter(
                    models.ScoreAnomaly.team_id == team_id,
                    models.ScoreAnomaly.judge_id == eval_record.judge_id
                ).first()

                if not existing_anomaly:
                    anomaly = models.ScoreAnomaly(
                        event_id=event_id,
                        team_id=team_id,
                        judge_id=eval_record.judge_id,
                        flagged_score=eval_record.weighted_total,
                        panel_average=panel_average,
                        deviation=deviation
                    )
                    db.add(anomaly)
                    db.commit()
                    db.refresh(anomaly)
                    
                    # Generate AI anomaly explanation
                    explanation = agent.explain_anomaly_ai(anomaly.id, db)
                    anomalies_flagged.append({
                        "anomaly_id": anomaly.id,
                        "judge_id": eval_record.judge_id,
                        "explanation": explanation
                    })
        
    return {
        "status": "success",
        "judge_id": judge_id,
        "consolidated_score": weighted_total,
        "anomalies_detected": anomalies_flagged
    }

# ==========================================
# 4. NEW AGENTIC LAYER ENDPOINTS
# ==========================================

class ChatRequest(BaseModel):
    message: str
    history: List[dict] = []

@app.post("/events/{event_id}/agent/chat")
def agent_chat(event_id: int, request: ChatRequest, db: Session = Depends(get_db)):
    """Conversational interface for managing/configuring the event."""
    # 1. Fetch Event
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    # 2. Run agent chat loop
    conv_agent = agent.ConversationalAgent()
    response = conv_agent.chat(event_id, request.message, request.history, db)
    
    # 3. Log agent interaction to Activity Log
    meta = {"actions_performed": [a["tool"] for a in response.get("actions", [])]}
    log = models.ActivityLog(
        event_id=event_id,
        action="AGENT_CHAT",
        actor="Committee",
        metadata_info=meta
    )
    db.add(log)
    db.commit()
    
    return response

@app.get("/events/{event_id}/agent/advisory")
def agent_advisory(event_id: int, db: Session = Depends(get_db)):
    """Gets diagnostic advisory from the agent about stage blockages."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
        
    advisory_text = agent.get_stage_advisory(event_id, db)
    return {"advisory": advisory_text}

@app.get("/events/{event_id}/communications")
def list_communications(
    event_id: int, 
    stage: str = Query(None), 
    status: str = Query(None), 
    db: Session = Depends(get_db)
):
    """Lists drafted or sent communication logs."""
    query = db.query(agent_models.CommunicationLog).filter(agent_models.CommunicationLog.event_id == event_id)
    if stage:
        query = query.filter(agent_models.CommunicationLog.stage == stage.lower())
    if status:
        query = query.filter(agent_models.CommunicationLog.status == status.lower())
        
    return query.all()

@app.post("/events/{event_id}/communications/{comm_id}/send")
def send_communication(event_id: int, comm_id: int, db: Session = Depends(get_db)):
    """Signs off and simulates sending a drafted communication."""
    comm = db.query(agent_models.CommunicationLog).filter(
        agent_models.CommunicationLog.id == comm_id,
        agent_models.CommunicationLog.event_id == event_id
    ).first()
    
    if not comm:
        raise HTTPException(status_code=404, detail="Communication log not found.")
        
    if comm.status == "sent":
        return {"status": "already_sent", "message": "This communication has already been sent."}
        
    comm.status = "sent"
    comm.sent_at = datetime.utcnow()
    db.commit()
    
    # Log action
    log = models.ActivityLog(
        event_id=event_id,
        action="COMMUNICATION_SENT",
        actor="Committee",
        metadata_info={"recipient": comm.recipient_email, "stage": comm.stage}
    )
    db.add(log)
    db.commit()
    
    return {"status": "success", "message": f"Successfully sent communication to {comm.recipient_email}."}

@app.get("/events/{event_id}/evaluations/anomalies/{anomaly_id}/explanation")
def get_anomaly_explanation(event_id: int, anomaly_id: int, db: Session = Depends(get_db)):
    """Gets the AI-generated explanation for a score anomaly."""
    explanation = db.query(agent_models.ScoreAnomalyExplanation).filter(
        agent_models.ScoreAnomalyExplanation.anomaly_id == anomaly_id
    ).first()
    
    if not explanation:
        # Try to generate it on the fly
        anomaly = db.query(models.ScoreAnomaly).filter(
            models.ScoreAnomaly.id == anomaly_id,
            models.ScoreAnomaly.event_id == event_id
        ).first()
        if not anomaly:
            raise HTTPException(status_code=404, detail="Anomaly not found.")
        explanation_text = agent.explain_anomaly_ai(anomaly_id, db)
        return {"explanation": explanation_text}
        
    return {"explanation": explanation.explanation}

@app.post("/events/{event_id}/evaluations/anomalies/{anomaly_id}/resolve")
def resolve_anomaly_route(
    event_id: int, 
    anomaly_id: int, 
    action: str = Body(..., embed=True), 
    db: Session = Depends(get_db)
):
    """Resolves a flagged score anomaly (action: 'dismiss' or 'override')."""
    res = agent.resolve_anomaly(event_id, anomaly_id, action, db)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
        
    # Log action
    log = models.ActivityLog(
        event_id=event_id,
        action="ANOMALY_RESOLVED",
        actor="Committee",
        metadata_info={"anomaly_id": anomaly_id, "resolution": action}
    )
    db.add(log)
    db.commit()
    
    return res

# Monkeypatch services dynamically to generate team rationales during team formation
import services
original_generate_teams = services.generate_teams_algorithmically

def agentic_generate_teams(event_id: int, db: Session):
    teams = original_generate_teams(event_id, db)
    # Generate rationales for each team
    for team in teams:
        agent_tasks.generate_team_rationale_task(team.id, event_id, db)
    return teams

services.generate_teams_algorithmically = agentic_generate_teams


# ==========================================
# 5. FRONTEND ROUTING & EXTRA UTILITIES
# ==========================================
from fastapi.responses import HTMLResponse

@app.get("/events/")
def list_events_api(db: Session = Depends(get_db)):
    """API endpoint to retrieve all available events."""
    events = db.query(models.Event).order_by(models.Event.id.desc()).all()
    return [{
        "id": e.id,
        "name": e.name,
        "state": e.state.value if hasattr(e.state, 'value') else e.state,
        "configuration": e.configuration
    } for e in events]

@app.get("/events/{event_id}")
def get_event_api(event_id: int, db: Session = Depends(get_db)):
    """API endpoint to retrieve a single event's details."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return {
        "id": event.id,
        "name": event.name,
        "state": event.state.value if hasattr(event.state, 'value') else event.state,
        "configuration": event.configuration
    }

@app.get("/events/{event_id}/teams", response_model=List[schemas.TeamResponse])
def get_event_teams_api(event_id: int, db: Session = Depends(get_db)):
    """API endpoint to retrieve all teams and members for an event."""
    teams = db.query(models.Team).filter(models.Team.event_id == event_id).all()
    return [schemas.TeamResponse.model_validate(t) for t in teams]

@app.get("/events/{event_id}/participants")
def get_event_participants(event_id: int, db: Session = Depends(get_db)):
    """API endpoint to retrieve all participants of an event with magic link token."""
    participants = db.query(models.Participant).filter(models.Participant.event_id == event_id).all()
    res = []
    
    from main import SECRET_KEY, ALGORITHM
    import jwt
    
    for p in participants:
        team_name = "Unassigned"
        if p.team_id:
            team = db.query(models.Team).filter(models.Team.id == p.team_id).first()
            if team:
                team_name = team.name
                
        payload = {
            "sub": str(p.id),
            "role": "participant",
            "event_id": p.event_id,
            "exp": datetime.utcnow() + timedelta(days=7)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        
        res.append({
            "id": p.id,
            "name": p.name,
            "email": p.email,
            "institution": p.profile_data.get("institution", "Unknown") if p.profile_data else "Unknown",
            "skills": p.profile_data.get("skills", []) if p.profile_data else [],
            "experience_level": p.profile_data.get("experience_level", 0) if p.profile_data else 0,
            "team_name": team_name,
            "token": token
        })
    return res

# Override D: Serve beautiful HTML page for Participant Portal
remove_route_by_path("/participants/portal/{token}", ["GET"])

@app.get("/participants/portal/{token}", response_class=HTMLResponse)
def participant_portal_status_override(token: str, db: Session = Depends(get_db)):
    """Decodes the participant JWT and renders a styled status portal page."""
    import jwt
    from main import SECRET_KEY, ALGORITHM
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        participant_id = int(payload.get("sub"))
    except Exception:
        return HTMLResponse(content="""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <title>Invalid Token - EventFlow</title>
            <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700&display=swap" rel="stylesheet">
            <style>
                body { font-family: 'Plus Jakarta Sans', sans-serif; background: #030306; color: #f8fafc; text-align: center; padding-top: 100px; }
                .card { background: rgba(255, 255, 255, 0.02); border: 1px solid rgba(255, 255, 255, 0.05); padding: 40px; border-radius: 20px; max-width: 500px; margin: auto; backdrop-filter: blur(20px); }
                h1 { color: #f43f5e; margin-bottom: 20px; }
                p { color: #94a3b8; line-height: 1.6; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Authentication Failed</h1>
                <p>Token has expired or is invalid. Please contact the event organizer for a new magic link.</p>
            </div>
        </body>
        </html>
        """, status_code=401)
        
    participant = db.query(models.Participant).filter(models.Participant.id == participant_id).first()
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")
        
    event = db.query(models.Event).filter(models.Event.id == participant.event_id).first()
    
    # Fetch Team Assignment (BUT ONLY IF APPROVED)
    team_info = "Assignment Pending"
    rationale = None
    teammates = []
    if participant.team_id:
        team = db.query(models.Team).filter(models.Team.id == participant.team_id).first()
        if team and team.is_approved == 1:
            team_info = team.name
            rationale = team.rationale
            teammates = [m.name for m in team.members if m.id != participant.id]
            
    # Log the login
    from main import log_activity
    log_activity(db, event.id, "PORTAL_ACCESSED", f"Participant_{participant.id}", {})
    
    # Render premium status page
    teammates_html = "".join([f"<li>{name}</li>" for name in teammates]) if teammates else "<li>No other teammates assigned yet</li>"
    
    team_section = ""
    if team_info == "Assignment Pending":
        team_section = f"""
        <div class="team-card pending">
            <h2>Team Assignment</h2>
            <div class="status-msg">Your team assignment is currently being processed by the AI coordinator and is awaiting organizer approval. Please check back shortly.</div>
        </div>
        """
    else:
        team_section = f"""
        <div class="team-card">
            <h2>Your Team: <span class="highlight">{team_info}</span></h2>
            <div class="teammates-box">
                <h3>Teammates</h3>
                <ul>
                    {teammates_html}
                </ul>
            </div>
            {f'''
            <div class="rationale-box">
                <h3>AI Formation Rationale</h3>
                <p>"{rationale}"</p>
            </div>
            ''' if rationale else ""}
        </div>
        """
        
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>EventFlow - Participant Status Portal</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700&family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-base: #030306;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --color-primary: hsl(250, 89%, 60%);
                --color-primary-light: hsl(250, 95%, 75%);
                --color-secondary: hsl(186, 100%, 45%);
                --glass-bg: rgba(8, 8, 12, 0.7);
                --glass-border: rgba(255, 255, 255, 0.05);
                --radius-lg: 20px;
                --radius-md: 14px;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Plus Jakarta Sans', sans-serif;
                background: var(--bg-base);
                background-image: 
                    radial-gradient(at 0% 0%, rgba(79, 70, 229, 0.15) 0px, transparent 55%),
                    radial-gradient(at 100% 0%, rgba(0, 240, 255, 0.05) 0px, transparent 50%),
                    linear-gradient(rgba(255, 255, 255, 0.005) 1px, transparent 1px);
                background-size: 100% 100%, 100% 100%, 40px 40px;
                color: var(--text-main);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }}
            .portal-container {{
                max-width: 600px;
                width: 100%;
                background: var(--glass-bg);
                border: 1px solid var(--glass-border);
                border-radius: var(--radius-lg);
                padding: 40px;
                backdrop-filter: blur(20px);
                box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.8);
                display: flex;
                flex-direction: column;
                gap: 24px;
            }}
            header {{
                border-bottom: 1px solid var(--glass-border);
                padding-bottom: 20px;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            header h1 {{
                font-family: 'Outfit', sans-serif;
                font-size: 28px;
                font-weight: 700;
                background: linear-gradient(135deg, var(--text-main), var(--color-primary-light));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            header p {{
                font-size: 14px;
                color: var(--text-muted);
            }}
            .event-badge-row {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--glass-border);
                padding: 12px 18px;
                border-radius: var(--radius-md);
                font-size: 14px;
            }}
            .stage-badge {{
                padding: 4px 10px;
                border-radius: 12px;
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                background: rgba(16, 185, 129, 0.15);
                border: 1px solid rgb(16, 185, 129);
                color: rgb(167, 243, 208);
            }}
            .team-card {{
                background: linear-gradient(135deg, rgba(79, 70, 229, 0.08) 0%, rgba(124, 58, 237, 0.03) 100%);
                border: 1px solid var(--glass-border);
                border-radius: var(--radius-md);
                padding: 24px;
                display: flex;
                flex-direction: column;
                gap: 16px;
            }}
            .team-card h2 {{
                font-size: 20px;
                font-family: 'Outfit', sans-serif;
            }}
            .highlight {{
                color: var(--color-secondary);
            }}
            .teammates-box h3, .rationale-box h3 {{
                font-size: 12px;
                text-transform: uppercase;
                letter-spacing: 1px;
                color: var(--color-primary-light);
                margin-bottom: 8px;
            }}
            .teammates-box ul {{
                list-style: none;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            .teammates-box li {{
                font-size: 14px;
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--glass-border);
                padding: 10px 14px;
                border-radius: 8px;
            }}
            .rationale-box p {{
                font-size: 14px;
                line-height: 1.6;
                font-style: italic;
                color: var(--text-main);
            }}
            .status-msg {{
                font-size: 14px;
                color: var(--text-muted);
                line-height: 1.5;
            }}
            footer {{
                text-align: center;
                font-size: 11px;
                color: var(--text-muted);
                margin-top: 10px;
            }}
        </style>
    </head>
    <body>
        <div class="portal-container">
            <header>
                <h1>Welcome, {participant.name}!</h1>
                <p>Your Texas Instruments Hackathon Status & Roster Portal</p>
            </header>
            <div class="event-badge-row">
                <span><strong>Event:</strong> {event.name}</span>
                <span class="stage-badge">{event.state.value if hasattr(event.state, 'value') else event.state}</span>
            </div>
            {team_section}
            <footer>
                EventFlow Coordinator AI • Texas Instruments Hackathon
            </footer>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

# Serve the Single Page Application dashboard at the root URL
remove_route_by_path("/", ["GET"]) # Remove health check
@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    """Serves the main single-page dashboard HTML application."""
    static_file_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(static_file_path):
        with open(static_file_path, "r", encoding="utf-8") as f:
            return f.read()
    return """
    <html>
        <head><title>EventFlow Dashboard Error</title></head>
        <body style="font-family: sans-serif; text-align: center; margin-top: 100px;">
            <h1>EventFlow Dashboard</h1>
            <p style="color: red;">Dashboard template static/index.html is missing. Please build the frontend.</p>
        </body>
    </html>
    """

