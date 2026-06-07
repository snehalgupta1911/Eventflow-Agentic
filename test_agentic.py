import os
import sys
import unittest
from fastapi.testclient import TestClient

# Ensure the local path is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set Gemini API key to a dummy value so it runs MockGeminiModel for testing if not set
if not os.environ.get("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = ""

from agent_main import app
import models
import agent_models
from database import SessionLocal, Base, engine

class TestEventFlowAgentic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Bind metadata to recreate clean sqlite database tables
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)
        cls.db = SessionLocal()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        # Clean up database file if created
        if os.path.exists("./eventflow.db"):
            try:
                os.remove("./eventflow.db")
            except Exception:
                pass

    def test_end_to_end_agentic_flow(self):
        print("\n--- Running End-to-End Agentic Flow Integration Test ---")
        
        # 1. Create Event
        print("1. Creating Event...")
        response = self.client.post("/events/?name=Test+Hackathon")
        self.assertEqual(response.status_code, 200)
        event = response.json()
        event_id = event["id"]
        print(f"   -> Event Created with ID: {event_id}")

        # 2. Upload Roster
        print("2. Uploading Roster...")
        roster = [
            {"name": "Alice ML", "email": "alice@example.com", "profile_data": {"skills": ["ML", "AI"], "experience_level": 5, "institution": "College A"}},
            {"name": "Bob Frontend", "email": "bob@example.com", "profile_data": {"skills": ["Frontend", "React"], "experience_level": 4, "institution": "College B"}},
            {"name": "Charlie Backend", "email": "charlie@example.com", "profile_data": {"skills": ["Backend", "API"], "experience_level": 3, "institution": "College C"}},
            {"name": "David UX", "email": "david@example.com", "profile_data": {"skills": ["UI/UX", "Design"], "experience_level": 5, "institution": "College A"}},
            {"name": "Eva Fullstack", "email": "eva@example.com", "profile_data": {"skills": ["Backend", "Frontend"], "experience_level": 4, "institution": "College B"}},
            {"name": "Frank Dev", "email": "frank@example.com", "profile_data": {"skills": ["Backend"], "experience_level": 2, "institution": "College C"}}
        ]
        response = self.client.post(f"/events/{event_id}/participants/", json=roster)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 6)
        print("   -> Roster Loaded successfully.")

        # Set judges in config
        db_event = self.db.query(models.Event).filter(models.Event.id == event_id).first()
        config = dict(db_event.configuration or {})
        config["judges"] = ["judge1@example.com", "judge2@example.com"]
        config["scoring_weights"] = {"technical": 0.5, "pitch": 0.5}
        config["anomaly_threshold"] = 3.0
        db_event.configuration = config
        self.db.commit()

        # 3. Form Teams (should trigger rationale generation in background)
        print("3. Triggering Team Formation...")
        response = self.client.post(f"/events/{event_id}/form-teams/")
        self.assertEqual(response.status_code, 200)
        
        # Verify that rationales were generated in the database
        teams = self.db.query(models.Team).filter(models.Team.event_id == event_id).all()
        self.assertGreater(len(teams), 0)
        for t in teams:
            self.assertIsNotNone(t.rationale)
            self.assertIn("complement", t.rationale.lower())
            print(f"   -> Team {t.name} Rationale: {t.rationale}")

        # 4. Approve Teams (should draft welcome communications)
        print("4. Approving Teams...")
        response = self.client.post(f"/events/{event_id}/approve-teams/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["new_event_state"], "active")
        
        # Check welcome logs in CommunicationLog table
        welcome_logs = self.db.query(agent_models.CommunicationLog).filter(
            agent_models.CommunicationLog.event_id == event_id,
            agent_models.CommunicationLog.stage == "welcome"
        ).all()
        self.assertEqual(len(welcome_logs), 6) # one draft per participant
        for log in welcome_logs:
            self.assertEqual(log.status, "draft")
            self.assertIn("Welcome", log.subject)
            self.assertIsNotNone(log.content)
        print("   -> Welcome emails drafted successfully for all participants.")

        # 5. Submit Evaluations (Trigger Score Anomaly Engine)
        print("5. Submitting Evaluations with Divergent Scores...")
        team_id = teams[0].id
        
        # Judge 1 scores Team 1
        res1 = self.client.post(f"/events/{event_id}/evaluations/", json={
            "team_id": team_id,
            "judge_id": "Judge A",
            "scores": {"technical": 9.0, "pitch": 9.0}
        })
        self.assertEqual(res1.status_code, 200)
        
        # Judge 2 scores Team 1 with a very low score
        res2 = self.client.post(f"/events/{event_id}/evaluations/", json={
            "team_id": team_id,
            "judge_id": "Judge B",
            "scores": {"technical": 2.0, "pitch": 2.0}
        })
        self.assertEqual(res2.status_code, 200)
        
        # Check that anomaly was generated
        anomalies = self.db.query(models.ScoreAnomaly).filter(models.ScoreAnomaly.event_id == event_id).all()
        self.assertGreater(len(anomalies), 0)
        print(f"   -> Flagged {len(anomalies)} score anomalies.")
        
        # Check that AI generated an explanation for the anomaly
        explanation = self.db.query(agent_models.ScoreAnomalyExplanation).filter(
            agent_models.ScoreAnomalyExplanation.anomaly_id == anomalies[0].id
        ).first()
        self.assertIsNotNone(explanation)
        self.assertIsNotNone(explanation.explanation)
        print(f"   -> AI Anomaly Explanation: {explanation.explanation}")

        # 6. Check Agent Stage Advisory
        print("6. Getting Agent Stage Advisory...")
        res_adv = self.client.get(f"/events/{event_id}/agent/advisory")
        self.assertEqual(res_adv.status_code, 200)
        advisory = res_adv.json()["advisory"]
        self.assertIsNotNone(advisory)
        print(f"   -> Agent Advisory:\n{advisory}")

        # 7. Test Conversational Agent (update rules via chat)
        print("7. Testing Conversational Agent Chat...")
        chat_req = {
            "message": "Set the team size rules to min 2 and max 5, and set max per institution to 2.",
            "history": []
        }
        res_chat = self.client.post(f"/events/{event_id}/agent/chat", json=chat_req)
        self.assertEqual(res_chat.status_code, 200)
        chat_res = res_chat.json()
        print(f"   -> Agent Reply: {chat_res['reply']}")
        self.assertEqual(len(chat_res["actions"]), 1)
        self.assertEqual(chat_res["actions"][0]["tool"], "update_event_rules")
        
        # Verify the database rules updated
        self.db.expire(db_event)
        db_event = self.db.query(models.Event).filter(models.Event.id == event_id).first()
        rules = db_event.configuration.get("rules", {})
        self.assertEqual(rules.get("team_size", {}).get("min"), 2)
        self.assertEqual(rules.get("team_size", {}).get("max"), 5)
        self.assertEqual(rules.get("diversity", {}).get("max_per_institution"), 2)
        print("   -> Rules successfully updated in DB through Agent chat.")

        # 8. Resolve Score Anomaly
        print("8. Resolving Score Anomaly...")
        res_resolve = self.client.post(
            f"/events/{event_id}/evaluations/anomalies/{anomalies[0].id}/resolve",
            json={"action": "dismiss"}
        )
        self.assertEqual(res_resolve.status_code, 200)
        self.assertEqual(res_resolve.json()["status"], "success")
        
        # Verify anomaly is resolved in DB
        self.db.expire(anomalies[0])
        anomaly_db = self.db.query(models.ScoreAnomaly).filter(models.ScoreAnomaly.id == anomalies[0].id).first()
        self.assertEqual(anomaly_db.resolved, 1)
        print("   -> Anomaly successfully resolved.")

        # 9. List and Send Communications
        print("9. Listing and sending communications...")
        res_comms = self.client.get(f"/events/{event_id}/communications")
        self.assertEqual(res_comms.status_code, 200)
        comms_list = res_comms.json()
        self.assertGreater(len(comms_list), 0)
        
        comm_id_to_send = comms_list[0]["id"]
        res_send = self.client.post(f"/events/{event_id}/communications/{comm_id_to_send}/send")
        self.assertEqual(res_send.status_code, 200)
        self.assertEqual(res_send.json()["status"], "success")
        
        # Verify sent status in DB
        self.db.expire_all()
        comm_db = self.db.query(agent_models.CommunicationLog).filter(agent_models.CommunicationLog.id == comm_id_to_send).first()
        self.assertEqual(comm_db.status, "sent")
        self.assertIsNotNone(comm_db.sent_at)
        print(f"   -> Communication {comm_id_to_send} sent successfully.")

        # 10. Verify Frontend Dashboard and API integrations
        print("10. Verifying Frontend Dashboard & new API endpoints...")
        
        # Test GET / (serve index.html)
        res_dashboard = self.client.get("/")
        self.assertEqual(res_dashboard.status_code, 200)
        self.assertIn("text/html", res_dashboard.headers.get("content-type", ""))
        self.assertIn("EventFlow AI - Organizer Dashboard", res_dashboard.text)
        print("   -> Dashboard HTML serves correctly.")
        
        # Test GET /events/
        res_events = self.client.get("/events/")
        self.assertEqual(res_events.status_code, 200)
        events_list = res_events.json()
        self.assertGreater(len(events_list), 0)
        self.assertEqual(events_list[0]["id"], event_id)
        print("   -> GET /events/ returns active events list.")

        # Test GET /events/{event_id}
        res_event_details = self.client.get(f"/events/{event_id}")
        self.assertEqual(res_event_details.status_code, 200)
        self.assertEqual(res_event_details.json()["name"], "Test Hackathon")
        print("   -> GET /events/{id} returns correct configuration details.")

        # Test GET /events/{event_id}/teams
        res_teams = self.client.get(f"/events/{event_id}/teams")
        self.assertEqual(res_teams.status_code, 200)
        self.assertGreater(len(res_teams.json()), 0)
        print("   -> GET /events/{id}/teams returns teams lists.")

        print("\n--- Test Completed Successfully! ---")

    def test_ollama_model_fallback_and_integration(self):
        print("\n--- Running Ollama Model Integration and Fallback Test ---")
        import agent_tasks
        from unittest.mock import patch
        
        # Save old environment
        old_use_ollama = os.environ.get("USE_OLLAMA")
        os.environ["USE_OLLAMA"] = "true"
        
        try:
            # 1. Get model when USE_OLLAMA=true
            model = agent_tasks.get_gemini_model()
            self.assertIsInstance(model, agent_tasks.OllamaModel)
            print("   -> Successfully retrieved OllamaModel with USE_OLLAMA=true")
            
            # 2. Test fallback when Ollama is unreachable (requests raises connection error)
            with patch("requests.post") as mock_post:
                mock_post.side_effect = Exception("Connection refused")
                # Prompt that would trigger a specific mock response
                response = model.generate_content("draft a short, energetic welcome email")
                self.assertIn("Welcome to the event!", response.text)
                print("   -> Graceful fallback to MockGeminiModel verified on connection failure.")
                
            # 3. Test successful Ollama response mapping
            with patch("requests.post") as mock_post:
                class MockResponse:
                    def json(self):
                        return {"response": "Custom local Ollama response."}
                    def raise_for_status(self):
                        pass
                mock_post.return_value = MockResponse()
                response = model.generate_content("hello")
                self.assertEqual(response.text, "Custom local Ollama response.")
                print("   -> Successful Ollama API response mapping verified.")
                
        finally:
            # Restore environment
            if old_use_ollama is not None:
                os.environ["USE_OLLAMA"] = old_use_ollama
            else:
                os.environ.pop("USE_OLLAMA", None)

if __name__ == "__main__":
    unittest.main()
