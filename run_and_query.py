import time
import subprocess
import requests
import json
import os

BASE_URL = "http://127.0.0.1:8000"

def run_demo():
    print("🚀 Starting EventFlow FastAPI Uvicorn Server in the background...")
    # Start the server on port 8000
    server_process = subprocess.Popen(
        ["venv/bin/uvicorn", "agent_main:app", "--host", "127.0.0.1", "--port", "8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for the server to spin up
    print("⏳ Waiting for server to initialize...")
    time.sleep(3.0)
    
    try:
        # 1. Check Health
        print("\n🏥 Checking Server Health...")
        health = requests.get(f"{BASE_URL}/")
        print(f"Response: {json.dumps(health.json(), indent=2)}")
        
        # 2. Create Event
        print("\n📅 Creating a new Event 'TI Hackathon'...")
        event_res = requests.post(f"{BASE_URL}/events/?name=TI+Hackathon")
        event = event_res.json()
        event_id = event["id"]
        print(f"Response: {json.dumps(event, indent=2)}")
        
        # 3. Check Event Rules (before update)
        print("\n🔍 Current event configuration (default):")
        print(json.dumps(event["configuration"], indent=2))
        
        # 4. Chat with the Conversational Agent
        print("\n💬 Chatting with the Conversational Agent to dynamically configure rules...")
        chat_payload = {
            "message": "Hey! Set up our hackathon rules: we want teams of 3-4, and judging scoring weights of technical (0.7) and pitch (0.3).",
            "history": []
        }
        chat_res = requests.post(f"{BASE_URL}/events/{event_id}/agent/chat", json=chat_payload)
        chat_data = chat_res.json()
        print(f"\n🤖 Agent Thought:\n{chat_data.get('thought', 'No thought logs')}")
        print(f"\n🤖 Agent Reply:\n{chat_data['reply']}")
        print(f"\n⚙️ Actions Performed by Agent:\n{json.dumps(chat_data.get('actions', []), indent=2)}")
        
        # 5. Verify configuration update in DB
        print("\n🔍 Fetching updated Event status from DB...")
        status_res = requests.get(f"{BASE_URL}/events/{event_id}/agent/advisory")
        print(f"Agent Stage Advisory:\n{status_res.json().get('advisory')}")
        
    except Exception as e:
        print(f"❌ Error during demo: {e}")
    finally:
        # Clean up the server process
        print("\n🛑 Stopping background Uvicorn server...")
        server_process.terminate()
        server_process.wait()
        print("Server stopped.")

if __name__ == "__main__":
    run_demo()
