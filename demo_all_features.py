import time
import subprocess
import requests
import json
import os

# Set local database if DATABASE_URL is not set
if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite:///./eventflow.db"

BASE_URL = "http://127.0.0.1:8000"

def show_features_demo():
    print("=" * 70)
    print("🎬 EVENTFLOW AGENTIC LAYER - FULL LIFECYCLE DEMONSTRATION")
    print("=" * 70)
    
    # Remove sqlite db file to ensure a clean run every time
    if os.path.exists("./eventflow.db"):
        try:
            os.remove("./eventflow.db")
        except Exception:
            pass

    print("\n🚀 Starting EventFlow server in the background...")
    server_process = subprocess.Popen(
        ["venv/bin/uvicorn", "agent_main:app", "--host", "127.0.0.1", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for the server to spin up
    time.sleep(5.0)
    
    try:
        # ----------------------------------------------------
        # FEATURE 1: Event Creation & Default Config
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        # We use a clean name and check if database is fresh
        print("1️⃣ FEATURE: Creating Event 'TI Case Championship'...")
        print("-" * 50)
        event_res = requests.post(f"{BASE_URL}/events/?name=TI+Case+Championship")
        event = event_res.json()
        event_id = event["id"]
        print(f"Created Event ID: {event_id}")
        print("Default Config:")
        print(json.dumps(event["configuration"], indent=2))

        # ----------------------------------------------------
        # FEATURE 2: Roster Loading
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("2️⃣ FEATURE: Loading Participant Roster...")
        print("-" * 50)
        roster = [
            {"name": "Aman ML", "email": f"aman.ml.{event_id}@example.com", "profile_data": {"skills": ["ML", "AI"], "experience_level": 5, "institution": "College A"}},
            {"name": "Bhavna React", "email": f"bhavna.fe.{event_id}@example.com", "profile_data": {"skills": ["Frontend", "React"], "experience_level": 4, "institution": "College B"}},
            {"name": "Chirag Backend", "email": f"chirag.be.{event_id}@example.com", "profile_data": {"skills": ["Backend", "FastAPI"], "experience_level": 3, "institution": "College C"}},
            {"name": "Divya UX", "email": f"divya.ux.{event_id}@example.com", "profile_data": {"skills": ["UI/UX", "Figma"], "experience_level": 5, "institution": "College A"}},
            {"name": "Eshan Django", "email": f"eshan.be.{event_id}@example.com", "profile_data": {"skills": ["Backend", "Django"], "experience_level": 4, "institution": "College B"}},
            {"name": "Fiza Flutter", "email": f"fiza.fe.{event_id}@example.com", "profile_data": {"skills": ["Frontend", "Flutter"], "experience_level": 2, "institution": "College C"}}
        ]
        roster_res = requests.post(f"{BASE_URL}/events/{event_id}/participants/", json=roster)
        print(f"Successfully loaded {len(roster_res.json())} participants into database.")

        # ----------------------------------------------------
        # FEATURE 3: Conversational Rules Configuration
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("3️⃣ FEATURE: Conversational Configuration (AI Agent Chat)")
        print("-" * 50)
        print("Sending request to coordinate team sizes and judging weights...")
        chat_payload = {
            "message": "Set the team size rules to min 2 and max 3. Set the score weights to technical (0.6) and pitch (0.4). Set anomaly threshold to 3.5.",
            "history": []
        }
        chat_res = requests.post(f"{BASE_URL}/events/{event_id}/agent/chat", json=chat_payload)
        chat_data = chat_res.json()
        print(f"Agent Reply:\n\"{chat_data['reply']}\"")
        print("\nActions Executed by Agent:")
        print(json.dumps(chat_data["actions"], indent=2))

        # ----------------------------------------------------
        # FEATURE 4: Team Formation & Rationales
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("4️⃣ FEATURE: Algorithmic Team Formation & AI Rationales")
        print("-" * 50)
        print("Forming teams and auto-generating rationales in background...")
        form_res = requests.post(f"{BASE_URL}/events/{event_id}/form-teams/")
        teams = form_res.json()
        print(f"Debug Teams JSON: {json.dumps(teams, indent=2)}")
        
        # Verify generated rationales
        print(f"Formed {len(teams)} teams.")
        for team in teams:
            # Re-fetch the team status to read rationale
            print(f"\n👉 {team.get('name', 'Unnamed')}:")
            print(f"   Rationale: {team.get('rationale', 'Rationale pending...')}")

        # ----------------------------------------------------
        # FEATURE 5: Team Approval & Welcome Drafts
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("5️⃣ FEATURE: Human Approval Gate & Communication Drafting")
        print("-" * 50)
        print("Approving proposed teams (should trigger welcome email drafting)...")
        app_res = requests.post(f"{BASE_URL}/events/{event_id}/approve-teams/")
        print(f"Response: {json.dumps(app_res.json(), indent=2)}")
        
        # View drafted communication logs
        comms_res = requests.get(f"{BASE_URL}/events/{event_id}/communications?stage=welcome")
        welcome_drafts = comms_res.json()
        print(f"\nDrafted Welcome Emails in Log: {len(welcome_drafts)} emails.")
        print(f"Snippet of first welcome email draft:")
        print(f"To: {welcome_drafts[0]['recipient_email']}")
        print(f"Subject: {welcome_drafts[0]['subject']}")
        print(f"Body:\n{welcome_drafts[0]['content']}")

        # ----------------------------------------------------
        # FEATURE 6: Score Submissions & Anomaly Engine
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("6️⃣ FEATURE: Score Anomaly Detection & AI Explanations")
        print("-" * 50)
        team_id = teams[0]["id"]
        
        # Set judges list and anomaly threshold in config to pass validations
        from database import SessionLocal
        import models
        db = SessionLocal()
        db_event = db.query(models.Event).filter(models.Event.id == event_id).first()
        config = dict(db_event.configuration or {})
        config["judges"] = ["judge1@example.com", "judge2@example.com"]
        config["scoring_weights"] = {"technical": 0.6, "pitch": 0.4}
        config["anomaly_threshold"] = 3.5
        db_event.configuration = config
        db.commit()
        db.close()

        print("Simulating score submissions for the first team...")
        print("Judge A submits a high score: technical (9.0), pitch (9.0)")
        requests.post(f"{BASE_URL}/events/{event_id}/evaluations/", json={
            "team_id": team_id,
            "judge_id": "Judge A",
            "scores": {"technical": 9.0, "pitch": 9.0}
        })
        
        print("Judge B submits a low score: technical (1.0), pitch (1.0)")
        eval_res = requests.post(f"{BASE_URL}/events/{event_id}/evaluations/", json={
            "team_id": team_id,
            "judge_id": "Judge B",
            "scores": {"technical": 1.0, "pitch": 1.0}
        })
        
        eval_data = eval_res.json()
        print("\nFlagged Anomaly Info from response:")
        print(json.dumps(eval_data.get("anomalies_detected", []), indent=2))

        # ----------------------------------------------------
        # FEATURE 7: Stage Advisory (Blocked State)
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("7️⃣ FEATURE: AI Stage Advisory Check (Blocked Status)")
        print("-" * 50)
        advisory_res = requests.get(f"{BASE_URL}/events/{event_id}/agent/advisory")
        print(advisory_res.json()["advisory"])

        # ----------------------------------------------------
        # FEATURE 8: Anomaly Resolution
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("8️⃣ FEATURE: Scoring Anomaly Resolution Gate")
        print("-" * 50)
        anomaly_id = eval_data["anomalies_detected"][0]["anomaly_id"]
        print(f"Resolving anomaly ID {anomaly_id} with action 'dismiss'...")
        resolve_res = requests.post(
            f"{BASE_URL}/events/{event_id}/evaluations/anomalies/{anomaly_id}/resolve",
            json={"action": "dismiss"}
        )
        print(f"Response: {json.dumps(resolve_res.json(), indent=2)}")

        # ----------------------------------------------------
        # FEATURE 9: Stage Advancement & Results Emails
        # ----------------------------------------------------
        print("\n" + "-" * 50)
        print("9️⃣ FEATURE: Event Stage Advancement & Results Drafting")
        print("-" * 50)
        print("Advancing stage to RESULTS_PENDING (no longer blocked)...")
        requests.patch(f"{BASE_URL}/events/{event_id}/stage", json={"next_stage": "results_pending"})
        
        print("Asking Conversational Agent to draft final results notification emails...")
        chat_payload = {
            "message": "We have resolved anomalies and advanced the stage. Please draft results notifications for all participants.",
            "history": []
        }
        chat_res = requests.post(f"{BASE_URL}/events/{event_id}/agent/chat", json=chat_payload)
        print(f"Agent Reply:\n\"{chat_res.json()['reply']}\"")
        
        # Verify drafts in DB
        res_comms = requests.get(f"{BASE_URL}/events/{event_id}/communications?stage=results_notification")
        results_drafts = res_comms.json()
        print(f"\nDrafted Results Emails: {len(results_drafts)} emails.")
        print(f"Snippet of first results email draft:")
        print(f"To: {results_drafts[0]['recipient_email']}")
        print(f"Subject: {results_drafts[0]['subject']}")
        print(f"Body:\n{results_drafts[0]['content']}\n")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Error in demo: {e}")
    finally:
        # Shut down background server process
        print("🛑 Terminating background Uvicorn server...")
        server_process.terminate()
        server_process.wait()
        print("Server successfully stopped.")
        print("=" * 70)

if __name__ == "__main__":
    show_features_demo()
