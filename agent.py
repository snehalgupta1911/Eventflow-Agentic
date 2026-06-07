import os
import json
import logging
from sqlalchemy.orm import Session
import models
import agent_models
import agent_tasks
import services
from agent_tasks import get_gemini_model

logger = logging.getLogger(__name__)

# ==========================================
# 1. TOOL DEFINITIONS (EXPOSED TO AI AGENT)
# ==========================================

def get_event_status(event_id: int, db: Session) -> dict:
    """Gets the current state, configuration, and statistics of the event."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return {"error": f"Event {event_id} not found."}
        
    participants_count = db.query(models.Participant).filter(models.Participant.event_id == event_id).count()
    unassigned_count = db.query(models.Participant).filter(
        models.Participant.event_id == event_id,
        models.Participant.team_id == None
    ).count()
    
    teams = db.query(models.Team).filter(models.Team.event_id == event_id).all()
    approved_teams_count = sum(1 for t in teams if t.is_approved == 1)
    
    anomalies_count = db.query(models.ScoreAnomaly).filter(
        models.ScoreAnomaly.event_id == event_id,
        models.ScoreAnomaly.resolved == 0
    ).count()
    
    drafted_comms_count = db.query(agent_models.CommunicationLog).filter(
        agent_models.CommunicationLog.event_id == event_id,
        agent_models.CommunicationLog.status == "draft"
    ).count()
    
    sent_comms_count = db.query(agent_models.CommunicationLog).filter(
        agent_models.CommunicationLog.event_id == event_id,
        agent_models.CommunicationLog.status == "sent"
    ).count()

    return {
        "event_id": event.id,
        "name": event.name,
        "state": event.state.value if hasattr(event.state, 'value') else event.state,
        "configuration": event.configuration,
        "participants": {
            "total": participants_count,
            "unassigned": unassigned_count
        },
        "teams": {
            "total": len(teams),
            "approved": approved_teams_count,
            "pending_approval": len(teams) - approved_teams_count
        },
        "anomalies": {
            "unresolved": anomalies_count
        },
        "communications": {
            "drafts": drafted_comms_count,
            "sent": sent_comms_count
        }
    }

def update_event_rules(event_id: int, rules: dict = None, db: Session = None, **kwargs) -> dict:
    """Updates the team formation rules (e.g. team_size: {min, max}, diversity constraints)."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return {"error": f"Event {event_id} not found."}
        
    if rules is None:
        rules = {}
        
    # Handle flat arguments if the model failed to nest them inside a rules dictionary
    if "team_size" in kwargs:
        rules["team_size"] = kwargs["team_size"]
    if "diversity" in kwargs:
        rules["diversity"] = kwargs["diversity"]
    if "min" in kwargs or "max" in kwargs:
        team_size = dict(rules.get("team_size") or {})
        if "min" in kwargs: team_size["min"] = kwargs["min"]
        if "max" in kwargs: team_size["max"] = kwargs["max"]
        rules["team_size"] = team_size
    if "max_per_institution" in kwargs:
        diversity = dict(rules.get("diversity") or {})
        diversity["max_per_institution"] = kwargs["max_per_institution"]
        rules["diversity"] = diversity
        
    # Copy configuration dict to trigger SQLAlchemy modification detection
    config = dict(event.configuration or {})
    current_rules = dict(config.get("rules", {}))
    
    # Merge rules
    for k, v in rules.items():
        if isinstance(v, dict) and k in current_rules and isinstance(current_rules[k], dict):
            nested = dict(current_rules[k])
            nested.update(v)
            current_rules[k] = nested
        else:
            current_rules[k] = v
            
    config["rules"] = current_rules
    event.configuration = config
    db.commit()
    db.refresh(event)
    
    return {"status": "success", "rules": event.configuration["rules"]}

def update_scoring_rules(event_id: int, scoring_weights: dict = None, anomaly_threshold: float = None, db: Session = None) -> dict:
    """Updates scoring weights and anomaly deviation threshold."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return {"error": f"Event {event_id} not found."}
        
    # Copy configuration dict to trigger SQLAlchemy modification detection
    config = dict(event.configuration or {})
    
    if scoring_weights:
        config["scoring_weights"] = scoring_weights
    if anomaly_threshold is not None:
        config["anomaly_threshold"] = anomaly_threshold
        
    event.configuration = config
    db.commit()
    
    return {
        "status": "success", 
        "scoring_weights": event.configuration.get("scoring_weights"),
        "anomaly_threshold": event.configuration.get("anomaly_threshold")
    }

def trigger_team_formation(event_id: int, db: Session) -> dict:
    """Triggers team formation and automatically starts background rationale generation."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return {"error": f"Event {event_id} not found."}
        
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

        # Run service team builder
        teams = services.generate_teams_algorithmically(event_id, db)
        
        # Start rationale generation for each team
        for team in teams:
            agent_tasks.generate_team_rationale_task(team.id, event_id, db)
            
        return {
            "status": "success",
            "message": f"Successfully formed {len(teams)} teams and queued rationale generation.",
            "teams_count": len(teams)
        }
    except ValueError as e:
        return {"error": str(e)}

def approve_teams(event_id: int, db: Session) -> dict:
    """Approves proposed teams, advances stage, and drafts welcome emails."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return {"error": f"Event {event_id} not found."}
        
    if event.state != models.EventState.TEAMS_PROPOSED:
        return {"error": f"Teams are not in proposed state. Current state is: {event.state.value if hasattr(event.state, 'value') else event.state}"}
        
    pending_teams = db.query(models.Team).filter(
        models.Team.event_id == event_id,
        models.Team.is_approved == 0
    ).all()
    
    if not pending_teams:
        return {"error": "No pending teams found to approve."}
        
    for team in pending_teams:
        team.is_approved = 1
        # Trigger welcome email drafting in background
        agent_tasks.draft_welcome_emails_task(team.id, event_id, db)
        
    event.state = models.EventState.ACTIVE
    db.commit()
    
    return {
        "status": "success",
        "message": f"Approved {len(pending_teams)} teams, advanced event to ACTIVE, and drafted welcome emails.",
        "new_state": event.state.value if hasattr(event.state, 'value') else event.state
    }

def resolve_anomaly(event_id: int, anomaly_id: int, action: str, db: Session) -> dict:
    """Resolves a flagged score anomaly (action: 'dismiss' or 'override')."""
    anomaly = db.query(models.ScoreAnomaly).filter(
        models.ScoreAnomaly.id == anomaly_id,
        models.ScoreAnomaly.event_id == event_id
    ).first()
    
    if not anomaly:
        return {"error": f"Anomaly {anomaly_id} not found for event {event_id}."}
        
    if action.lower() == "dismiss":
        anomaly.resolved = 1
        db.commit()
        return {"status": "success", "message": f"Anomaly {anomaly_id} dismissed."}
    elif action.lower() == "override":
        # Overrides the judge's score to match the panel average
        evaluation = db.query(models.Evaluation).filter(
            models.Evaluation.event_id == event_id,
            models.Evaluation.team_id == anomaly.team_id,
            models.Evaluation.judge_id == anomaly.judge_id
        ).first()
        
        if evaluation:
            # Shift score to panel average
            evaluation.weighted_total = anomaly.panel_average
            
        anomaly.resolved = 1
        db.commit()
        return {"status": "success", "message": f"Anomaly {anomaly_id} resolved by overriding score to panel average ({anomaly.panel_average:.2f})."}
    else:
        return {"error": f"Invalid resolution action '{action}'. Must be 'dismiss' or 'override'."}

def advance_stage(event_id: int, next_stage: str, db: Session) -> dict:
    """Advances the event to the specified stage with safety validation checks."""
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return {"error": "Event not found"}

    requested = next_stage.upper()
    
    # Validation checks
    if requested == "RESULTS_PENDING":
        unresolved_anomalies = db.query(models.ScoreAnomaly).filter(
            models.ScoreAnomaly.event_id == event_id,
            models.ScoreAnomaly.resolved == 0
        ).count()
        if unresolved_anomalies > 0:
            return {"error": f"Cannot advance stage. There are {unresolved_anomalies} unresolved anomalies."}
            
    elif requested == "ACTIVE":
        unapproved_teams = db.query(models.Team).filter(
            models.Team.event_id == event_id,
            models.Team.is_approved == 0
        ).count()
        if unapproved_teams > 0:
            return {"error": "Cannot start event. All proposed teams must be approved first."}

    try:
        # Check mapping of string to enum
        for estate in models.EventState:
            if estate.value == requested.lower() or estate.name == requested:
                event.state = estate
                db.commit()
                return {"status": "success", "new_stage": event.state.value if hasattr(event.state, 'value') else event.state}
        
        # fallback string update if enum match fails
        event.state = requested
        db.commit()
        return {"status": "success", "new_stage": requested}
    except Exception as e:
        return {"error": f"Failed to transition stage: {str(e)}"}

def draft_communications(event_id: int, stage: str, db: Session) -> dict:
    """Drafts communications (emails) for a specific stage ('welcome', 'evaluation_reminder', 'results_notification', 'progression_invitation')."""
    stage = stage.lower()
    if stage == "welcome":
        # Draft welcome emails for all approved teams
        teams = db.query(models.Team).filter(models.Team.event_id == event_id, models.Team.is_approved == 1).all()
        for t in teams:
            agent_tasks.draft_welcome_emails_task(t.id, event_id, db)
        return {"status": "success", "message": f"Drafted welcome emails for {len(teams)} approved teams."}
    elif stage == "evaluation_reminder":
        agent_tasks.draft_evaluation_reminders_task(event_id, db)
        return {"status": "success", "message": "Drafted evaluation reminders for all judges."}
    elif stage == "results_notification":
        agent_tasks.draft_results_notifications_task(event_id, db)
        return {"status": "success", "message": "Drafted results notifications for all participants."}
    elif stage == "progression_invitation":
        agent_tasks.draft_progression_invitations_task(event_id, db)
        return {"status": "success", "message": "Drafted progression invitations for the top 5 qualifying teams."}
    else:
        return {"error": f"Unknown communication stage '{stage}'. Available: welcome, evaluation_reminder, results_notification, progression_invitation."}

def send_approved_communications(event_id: int, stage: str, db: Session) -> dict:
    """Sends all approved communications (drafts) for the specified stage."""
    from datetime import datetime
    comms = db.query(agent_models.CommunicationLog).filter(
        agent_models.CommunicationLog.event_id == event_id,
        agent_models.CommunicationLog.stage == stage.lower(),
        agent_models.CommunicationLog.status == "draft"
    ).all()
    
    if not comms:
        return {"status": "success", "message": f"No draft communications found to send for stage '{stage}'."}
        
    for comm in comms:
        comm.status = "sent"
        comm.sent_at = datetime.utcnow()
        
    db.commit()
    return {"status": "success", "message": f"Successfully sent {len(comms)} communications for stage '{stage}'."}


# ==========================================
# 2. AI SCORE ANOMALY EXPLAINER
# ==========================================

def explain_anomaly_ai(anomaly_id: int, db: Session) -> str:
    """Uses Gemini to generate a human-readable explanation of why a score was flagged."""
    anomaly = db.query(models.ScoreAnomaly).filter(models.ScoreAnomaly.id == anomaly_id).first()
    if not anomaly:
        return "Anomaly not found."
        
    # Fetch team name
    team = db.query(models.Team).filter(models.Team.id == anomaly.team_id).first()
    team_name = team.name if team else f"Team {anomaly.team_id}"
    
    prompt = f"""
    You are an event organizer's AI assistant. A score anomaly was flagged for {team_name}.
    Here is the data:
    - Judge Name/ID: {anomaly.judge_id}
    - Judge's Consolidated Score: {anomaly.flagged_score:.2f}
    - Panel Average Score (excluding or including all): {anomaly.panel_average:.2f}
    - Absolute Deviation: {anomaly.deviation:.2f}
    
    Write a clear, concise 2-sentence explanation of this discrepancy for the committee dashboard. 
    Explain that this judge's score is significantly higher/lower than the average, indicating a possible scoring bias or logging typo, and suggest that the committee review or override it.
    Do not use introductory text. Write the explanation directly.
    """
    
    explanation_text = "Divergent score flagged."
    try:
        model = get_gemini_model()
        response = model.generate_content(prompt)
        explanation_text = response.text.strip()
    except Exception as e:
        logger.error(f"Failed to generate AI anomaly explanation: {e}")
        explanation_text = f"Judge {anomaly.judge_id}'s score of {anomaly.flagged_score:.1f} deviates from the panel average of {anomaly.panel_average:.1f} by {anomaly.deviation:.1f} points."

    # Save to database
    existing = db.query(agent_models.ScoreAnomalyExplanation).filter(
        agent_models.ScoreAnomalyExplanation.anomaly_id == anomaly_id
    ).first()
    
    if existing:
        existing.explanation = explanation_text
    else:
        db_explanation = agent_models.ScoreAnomalyExplanation(
            anomaly_id=anomaly_id,
            explanation=explanation_text
        )
        db.add(db_explanation)
        
    db.commit()
    return explanation_text


# ==========================================
# 3. AI STAGE ADVISOR
# ==========================================

def get_stage_advisory(event_id: int, db: Session) -> str:
    """Returns a plain-English advisory on current stage blockages and recommendations."""
    status = get_event_status(event_id, db)
    if "error" in status:
        return status["error"]
        
    prompt = f"""
    You are the operational manager for an event. Here is the current diagnostic status of the event:
    - Name: {status['name']}
    - Current Stage: {status['state']}
    - Participants: {status['participants']['total']} total, {status['participants']['unassigned']} unassigned
    - Teams: {status['teams']['total']} total, {status['teams']['approved']} approved, {status['teams']['pending_approval']} pending
    - Score Anomalies: {status['anomalies']['unresolved']} unresolved
    - Communications: {status['communications']['drafts']} draft emails pending approval, {status['communications']['sent']} emails sent
    
    Write a 3-sentence operational advisory:
    1. Summarize where the event stands.
    2. State clearly what actions are currently blocked (e.g., cannot start judging if teams are unapproved, cannot finalize results if anomalies are unresolved).
    3. Recommend the exact next step the committee should take (e.g., form teams, approve rosters, resolve anomalies, sign off on communications).
    Keep it concise, actionable, and formatted with clear spacing.
    """
    
    try:
        model = get_gemini_model()
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Failed to generate stage advisory: {e}")
        # fallback
        return f"Event is currently in state {status['state'].upper()}. {status['participants']['unassigned']} participants remain unassigned. {status['anomalies']['unresolved']} unresolved anomalies exist."


# ==========================================
# 4. CONVERSATIONAL COMMITTEE AGENT
# ==========================================

class ConversationalAgent:
    def __init__(self):
        # Maps string function names to actual Python functions
        self.tools = {
            "get_event_status": get_event_status,
            "update_event_rules": update_event_rules,
            "update_scoring_rules": update_scoring_rules,
            "trigger_team_formation": trigger_team_formation,
            "approve_teams": approve_teams,
            "resolve_anomaly": resolve_anomaly,
            "advance_stage": advance_stage,
            "draft_communications": draft_communications,
            "send_approved_communications": send_approved_communications
        }

    def chat(self, event_id: int, message: str, history: list, db: Session) -> dict:
        """
        Processes a conversation turn, dynamically runs tools, and replies.
        history format: [{"role": "user"/"assistant", "content": "..."}]
        """
        # Fetch current status to provide context to the LLM
        status = get_event_status(event_id, db)
        
        # Build prompt with tool declarations
        tool_descriptions = """
        You have access to the following tools to manage the event:
        
        1. `get_event_status(event_id: int)`: Inspects overall event details (stage, participant count, teams, anomalies).
        2. `update_event_rules(event_id: int, rules: dict)`: Updates team formation rules. Arguments: `rules` (e.g. {"team_size": {"min": 3, "max": 4}, "diversity": {"max_per_institution": 1}}).
        3. `update_scoring_rules(event_id: int, scoring_weights: dict, anomaly_threshold: float)`: Sets consolidated judging weights and deviation threshold. E.g. scoring_weights: {"technical": 0.6, "pitch": 0.4}, anomaly_threshold: 10.0.
        4. `trigger_team_formation(event_id: int)`: Forms teams algorithmically.
        5. `approve_teams(event_id: int)`: Approves teams and drafts welcome communications.
        6. `resolve_anomaly(event_id: int, anomaly_id: int, action: str)`: Resolves an anomaly. Arguments: `anomaly_id` (int), `action` ("dismiss" or "override").
        7. `advance_stage(event_id: int, next_stage: str)`: Transitions stage. Next stages: "setup", "roster_loaded", "teams_proposed", "active", "results_pending", "completed".
        8. `draft_communications(event_id: int, stage: str)`: Drafts emails. Stages: "welcome", "evaluation_reminder", "results_notification", "progression_invitation".
        9. `send_approved_communications(event_id: int, stage: str)`: Signs off and sends drafted emails. Stages: "welcome", "evaluation_reminder", "results_notification", "progression_invitation".
        """

        system_instruction = f"""
        You are EventFlow AI, the conversational coordinator for the competitive event management system.
        You are interacting with the event's core organizing committee.
        
        Context of current event:
        {json.dumps(status, indent=2)}
        
        {tool_descriptions}
        
        Your output MUST be a valid JSON object matching the following structure:
        {{
            "thought": "Reasoning about the user's intent and whether a tool call is needed.",
            "tool_call": {{
                "name": "Name of the tool to execute (omit this field if no tool call is needed)",
                "args": {{ ... arguments for the tool ... }}
            }},
            "reply": "Conversational reply to the user. Explain what actions you are performing or ask clarifying questions if parameters are missing."
        }}
        
        Strict rules:
        - If the user specifies rules (e.g. team size, diversity) or asks to do an action (e.g. form teams, approve, resolve anomaly), translate it to the appropriate tool call.
        - If parameters for a tool are missing (e.g., the user says "set up team rules" but didn't state sizes), do NOT invoke the tool. Use your "reply" to ask for clarification.
        - Never invent tools. Only use the listed ones.
        """

        # Build prompt from history
        messages_formatted = []
        for h in history:
            messages_formatted.append(f"{h['role'].upper()}: {h['content']}")
        messages_formatted.append(f"USER: {message}")
        full_conversation = "\n".join(messages_formatted)

        prompt = f"""
        {system_instruction}
        
        Conversation history:
        {full_conversation}
        
        Output JSON object:
        """

        actions_performed = []
        final_reply = "I'm sorry, I encountered an issue processing that request."

        try:
            model = get_gemini_model()
            response = model.generate_content(prompt)
            content_text = response.text.strip()
            
            # Clean up markdown formatting if the model wrapped it in ```json
            if content_text.startswith("```json"):
                content_text = content_text[7:]
            if content_text.endswith("```"):
                content_text = content_text[:-3]
            content_text = content_text.strip()

            parsed = json.loads(content_text)
            final_reply = parsed.get("reply", "")
            
            # Check for tool call
            tool_call = parsed.get("tool_call")
            if tool_call and tool_call.get("name"):
                tool_name = tool_call["name"]
                tool_args = tool_call.get("args", {})
                
                if tool_name in self.tools:
                    # Inject event_id and db
                    tool_args["event_id"] = event_id
                    tool_args["db"] = db
                    
                    # Execute tool
                    tool_func = self.tools[tool_name]
                    result = tool_func(**tool_args)
                    
                    # Record action
                    actions_performed.append({
                        "tool": tool_name,
                        "args": {k: v for k, v in tool_args.items() if k not in ["db", "event_id"]},
                        "result": result
                    })
                    
                    # Perform a second turn so the agent can report back the result of the tool execution
                    new_context = f"""
                    Tool execution result:
                    {json.dumps(result, indent=2)}
                    
                    Formulate a final conversational response to the user reporting the outcome of this action.
                    """
                    
                    second_prompt = f"""
                    {system_instruction}
                    
                    Conversation:
                    {full_conversation}
                    
                    {new_context}
                    
                    Output JSON object:
                    """
                    
                    response2 = model.generate_content(second_prompt)
                    content2 = response2.text.strip()
                    if content2.startswith("```json"):
                        content2 = content2[7:]
                    if content2.endswith("```"):
                        content2 = content2[:-3]
                    content2 = content2.strip()
                    
                    parsed2 = json.loads(content2)
                    final_reply = parsed2.get("reply", final_reply)
                else:
                    final_reply = f"The agent tried to call a tool named '{tool_name}' which is not registered."
                    
        except json.JSONDecodeError as jde:
            logger.error(f"Failed to parse agent JSON response: {jde}. Raw text: {content_text}")
            # Fallback simple text parser
            final_reply = content_text
        except Exception as e:
            logger.error(f"Error in ConversationalAgent: {e}")
            final_reply = f"Error processing message: {str(e)}"

        return {
            "reply": final_reply,
            "actions": actions_performed
        }
