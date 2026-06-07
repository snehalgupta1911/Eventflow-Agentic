import os
import logging
from sqlalchemy.orm import Session
import models
import agent_models

logger = logging.getLogger(__name__)

# Configure Gemini or fallback to a Mock
class MockGeminiModel:
    def generate_content(self, prompt, **kwargs):
        class MockResponse:
            def __init__(self, text):
                self.text = text
        
        import json
        prompt_lower = prompt.lower()
        
        # 1. Score Anomaly Explainer
        if "score anomaly was flagged for" in prompt_lower:
            return MockResponse("The score submitted by Judge B is extremely low (2.0) compared to the panel average of 5.5. This significant deviation of 3.5 points suggests either a scoring discrepancy or a typo that the committee should review.")
            
        # 2. Stage Advisory
        elif "operational manager for an event. here is the current diagnostic status" in prompt_lower:
            return MockResponse("The event is currently in the ACTIVE stage with 6 participants in 2 teams. However, 2 unresolved score anomalies are blocking progression to the results phase. The committee should resolve these anomalies and approve any pending communications to move forward.")
            
        # 3. Conversational Agent - Second Turn (Reporting Tool Success)
        elif "tool execution result" in prompt_lower:
            user_lines = [line for line in prompt_lower.split("\n") if "user:" in line]
            user_text = " ".join(user_lines) if user_lines else ""
            if "draft" in user_text or "results" in user_text:
                return MockResponse(json.dumps({
                    "thought": "The results notifications were successfully drafted. I will inform the user.",
                    "reply": "I have successfully drafted the results notifications for all participants."
                }))
            else:
                return MockResponse(json.dumps({
                    "thought": "The tool successfully executed. I will let the user know.",
                    "reply": "I have successfully updated the team size rules and diversity constraints as requested."
                }))
            
        # 4. Conversational Agent - First Turn (Tool Calling)
        elif "eventflow ai, the conversational coordinator" in prompt_lower:
            user_lines = [line for line in prompt_lower.split("\n") if "user:" in line]
            user_text = " ".join(user_lines) if user_lines else ""
            if "draft" in user_text or "results" in user_text:
                return MockResponse(json.dumps({
                    "thought": "The user wants to draft results notifications for all participants.",
                    "tool_call": {
                        "name": "draft_communications",
                        "args": {
                            "stage": "results_notification"
                        }
                    },
                    "reply": "I am drafting results notifications for all participants."
                }))
            else:
                return MockResponse(json.dumps({
                    "thought": "The user wants to update the team size rules to min 2, max 5, and max per institution to 2.",
                    "tool_call": {
                        "name": "update_event_rules",
                        "args": {
                            "rules": {
                                "team_size": {"min": 2, "max": 5},
                                "diversity": {"max_per_institution": 2}
                            }
                        }
                    },
                    "reply": "I am updating the team size rules to min 2, max 5, and diversity constraints to max 2 per institution."
                }))
            
        # 5. Welcome Email Draft
        elif "draft a short, energetic welcome email" in prompt_lower:
            return MockResponse("Welcome to the event! We are excited to have you on board. Your team has been formed based on your complementary skills in design and coding. Please log in to your portal to get started.")
            
        # 6. Evaluation Reminder Draft
        elif "draft a professional, clear evaluation reminder email" in prompt_lower:
            return MockResponse("Dear Judge, this is a reminder that the evaluation phase has started. Please review the teams and submit your scores using the evaluation dashboard.")
            
        # 7. Results Notification Draft
        elif "draft a polite, inspiring results notification email" in prompt_lower:
            return MockResponse("Dear Participant, the results are out! Thank you for participating. You can check the final leaderboard on your status portal.")
            
        # 8. Progression Invitation Draft
        elif "draft an official, exciting progression invitation email" in prompt_lower:
            return MockResponse("Congratulations! Your team has qualified for the next round. Please log in to your portal to confirm your acceptance of the progression invitation.")
            
        # 9. Rationale Generation
        elif "rationale explaining why this specific combination of participants is strong" in prompt_lower:
            return MockResponse("This team combines strong backend development with frontend design skills, which complement each other and ensure a balanced implementation that satisfies all diversity constraints.")
            
        # Default fallback
        else:
            return MockResponse("Mock response for prompt: " + str(prompt[:50]))

def get_gemini_model():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not found in environment. Using MockGeminiModel.")
        return MockGeminiModel()
        
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        
        target_model = "gemini-1.5-flash"
            
        generation_config = {
            "temperature": 0.4,
            "max_output_tokens": 1024,
        }
        return genai.GenerativeModel(model_name=target_model, generation_config=generation_config)
    except Exception as e:
        logger.error(f"Error initializing Gemini API: {e}. Falling back to MockGeminiModel.")
        return MockGeminiModel()

# Core tasks

def generate_team_rationale_task(team_id: int, event_id: int, db: Session):
    """
    Background task to generate an LLM rationale for a specific team.
    """
    team = db.query(models.Team).filter(models.Team.id == team_id).first()
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    
    if not team or not event:
        return
        
    rules = event.configuration.get("rules", {})
    
    member_descriptions = []
    for member in team.members:
        skills = member.profile_data.get("skills", "Unknown")
        exp = member.profile_data.get("experience_level", "Unknown")
        inst = member.profile_data.get("institution", "Unknown")
        member_descriptions.append(f"- {member.name}: Skills: {skills}, Level: {exp}, College: {inst}")
        
    team_context = "\n".join(member_descriptions)
    
    prompt = f"""
    You are an AI assistant for a hackathon orchestration system.
    The committee has formed a team based on the following rules: {rules}
    
    Here is the team composition:
    {team_context}
    
    Write a concise, 2-sentence rationale explaining why this specific combination of participants is strong, how their skills complement each other, and how they satisfy the rules. 
    Do not use introductory filler (like "Here is the rationale"). Just provide the rationale directly.
    """
    
    try:
        model = get_gemini_model()
        response = model.generate_content(prompt)
        team.rationale = response.text.strip()
        db.commit()
        logger.info(f"Generated rationale for Team {team_id} successfully.")
    except Exception as e:
        logger.error(f"Failed to generate rationale for Team {team_id}: {e}")

def draft_welcome_emails_task(team_id: int, event_id: int, db: Session):
    """
    Background task to draft personalized welcome emails for all members of an approved team.
    """
    team = db.query(models.Team).filter(models.Team.id == team_id).first()
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    
    if not team or not event:
        return

    model = get_gemini_model()

    for member in team.members:
        # Check if already drafted
        existing = db.query(agent_models.CommunicationLog).filter(
            agent_models.CommunicationLog.event_id == event_id,
            agent_models.CommunicationLog.recipient_email == member.email,
            agent_models.CommunicationLog.stage == "welcome"
        ).first()
        if existing:
            continue

        member_details = ", ".join([
            f"{m.name} ({', '.join(m.profile_data.get('skills', ['Hacker']))})" 
            for m in team.members
        ])

        prompt = f"""
        You are the automated communication system for the '{event.name}' event.
        The committee has just approved the team '{team.name}'.
        Recipient: {member.name} (Email: {member.email})
        All Team Members: {member_details}.
        Rationale for this grouping: {team.rationale}
        
        Draft a short, energetic welcome email (max 3 paragraphs) to be sent to this participant.
        - Welcome them to the event.
        - Briefly explain why their team is strong (using the rationale).
        - Tell them their next step is to log into their participant portal.
        Do not include subject lines or placeholder brackets like [Your Name]. Just the email body.
        """
        
        try:
            response = model.generate_content(prompt)
            content = response.text.strip()
            
            log = agent_models.CommunicationLog(
                event_id=event_id,
                recipient_email=member.email,
                recipient_role="participant",
                subject=f"Welcome to {event.name}! You are in {team.name}",
                content=content,
                stage="welcome",
                status="draft"
            )
            db.add(log)
        except Exception as e:
            logger.error(f"Failed to draft welcome email for {member.email}: {e}")
            
    db.commit()

def draft_evaluation_reminders_task(event_id: int, db: Session):
    """
    Background task to draft evaluation reminders for judges.
    """
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return

    # Fetch judges list from configuration, fallback to dummy list
    judges = event.configuration.get("judges", ["judge1@example.com", "judge2@example.com"])
    model = get_gemini_model()

    for judge in judges:
        existing = db.query(agent_models.CommunicationLog).filter(
            agent_models.CommunicationLog.event_id == event_id,
            agent_models.CommunicationLog.recipient_email == judge,
            agent_models.CommunicationLog.stage == "evaluation_reminder"
        ).first()
        if existing:
            continue

        prompt = f"""
        You are the coordinator for the '{event.name}' event.
        We have advanced to the Evaluation phase.
        Recipient: {judge} (Role: Judge/Mentor)
        
        Draft a professional, clear evaluation reminder email (max 2 paragraphs) to the judge.
        - Remind them that evaluation has started.
        - Instruct them to access their judging dashboard.
        - Emphasize the importance of submitting scores based on the evaluation criteria: {event.configuration.get('scoring_weights', {'technical': 1.0})}
        Do not include subject lines or placeholder brackets. Just the email body.
        """
        
        try:
            response = model.generate_content(prompt)
            content = response.text.strip()
            
            log = agent_models.CommunicationLog(
                event_id=event_id,
                recipient_email=judge,
                recipient_role="judge",
                subject=f"Action Required: Evaluation Phase Started for {event.name}",
                content=content,
                stage="evaluation_reminder",
                status="draft"
            )
            db.add(log)
        except Exception as e:
            logger.error(f"Failed to draft evaluation email for {judge}: {e}")
            
    db.commit()

def draft_results_notifications_task(event_id: int, db: Session):
    """
    Background task to draft final results notification emails for all participants.
    """
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return

    # Get leaderboard representation
    # Sort teams by average score
    teams = db.query(models.Team).filter(models.Team.event_id == event_id).all()
    team_scores = []
    for team in teams:
        evals = db.query(models.Evaluation).filter(models.Evaluation.team_id == team.id).all()
        avg_score = sum(e.weighted_total for e in evals) / len(evals) if evals else 0.0
        team_scores.append((team.name, avg_score))
        
    team_scores.sort(key=lambda x: x[1], reverse=True)
    leaderboard_str = "\n".join([f"{rank+1}. {name} (Score: {score:.2f})" for rank, (name, score) in enumerate(team_scores)])

    model = get_gemini_model()
    participants = db.query(models.Participant).filter(models.Participant.event_id == event_id).all()

    for participant in participants:
        existing = db.query(agent_models.CommunicationLog).filter(
            agent_models.CommunicationLog.event_id == event_id,
            agent_models.CommunicationLog.recipient_email == participant.email,
            agent_models.CommunicationLog.stage == "results_notification"
        ).first()
        if existing:
            continue

        prompt = f"""
        You are the automated notification assistant for '{event.name}'.
        The final evaluations are complete and results are ready.
        Recipient: {participant.name}
        
        Here is the top leaderboard standings:
        {leaderboard_str}
        
        Draft a polite, inspiring results notification email (max 3 paragraphs).
        - Announce the completion of the event.
        - Share the rankings and congratulate the top teams.
        - Thank everyone for their hard work.
        - Direct them to view their final scores in the status portal.
        Do not include subject lines or placeholder brackets. Just the email body.
        """
        
        try:
            response = model.generate_content(prompt)
            content = response.text.strip()
            
            log = agent_models.CommunicationLog(
                event_id=event_id,
                recipient_email=participant.email,
                recipient_role="participant",
                subject=f"Final Standings and Results for {event.name}",
                content=content,
                stage="results_notification",
                status="draft"
            )
            db.add(log)
        except Exception as e:
            logger.error(f"Failed to draft results notification for {participant.email}: {e}")
            
    db.commit()

def draft_progression_invitations_task(event_id: int, db: Session):
    """
    Background task to draft progression invitations for top 5 qualifying teams.
    """
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return

    # Find top 5 teams
    teams = db.query(models.Team).filter(models.Team.event_id == event_id).all()
    team_scores = []
    for team in teams:
        evals = db.query(models.Evaluation).filter(models.Evaluation.team_id == team.id).all()
        avg_score = sum(e.weighted_total for e in evals) / len(evals) if evals else 0.0
        team_scores.append((team, avg_score))
        
    team_scores.sort(key=lambda x: x[1], reverse=True)
    top_5_teams = [t[0] for t in team_scores[:5]]

    model = get_gemini_model()

    for team in top_5_teams:
        for member in team.members:
            existing = db.query(agent_models.CommunicationLog).filter(
                agent_models.CommunicationLog.event_id == event_id,
                agent_models.CommunicationLog.recipient_email == member.email,
                agent_models.CommunicationLog.stage == "progression_invitation"
            ).first()
            if existing:
                continue

            prompt = f"""
            You are the organizing committee for '{event.name}'.
            The team '{team.name}' has finished in the top standings and is invited to progress to the next round.
            Recipient: {member.name}
            
            Draft an official, exciting progression invitation email (max 3 paragraphs).
            - Congratulate them on qualifying.
            - Formally invite them to the next phase/final demo day.
            - Ask them to log into their participant portal to RSVP / confirm their attendance.
            Do not include subject lines or placeholder brackets. Just the email body.
            """
            
            try:
                response = model.generate_content(prompt)
                content = response.text.strip()
                
                log = agent_models.CommunicationLog(
                    event_id=event_id,
                    recipient_email=member.email,
                    recipient_role="participant",
                    subject=f"Action Required: Invitation to Progress - {event.name}",
                    content=content,
                    stage="progression_invitation",
                    status="draft"
                )
                db.add(log)
            except Exception as e:
                logger.error(f"Failed to draft progression email for {member.email}: {e}")
                
    db.commit()
