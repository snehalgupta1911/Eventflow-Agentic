# EventFlow Backend Engine 🚀

A high-performance, deterministic orchestration engine for hackathons and massive events. Built with Python, FastAPI, and PostgreSQL. 

This backend handles the heavy lifting: ingesting messy CSV data, mathematically balancing teams using O(n²) constraint satisfaction, calculating weighted judging scores, and acting as a strict state machine to prevent unauthorized event advancement.

## 🛠 Tech Stack
* **Framework:** FastAPI (Python 3.10+)
* **Database:** PostgreSQL (via Supabase) / SQLite (local fallback)
* **ORM:** SQLAlchemy + Pydantic
* **AI/LLM:** Google Gemini API (`google-generativeai`) / Local Open-Source Models (via Ollama)

---

## 🚀 Getting Started (Local Development)

### 1. Clone & Setup Virtual Environment
```bash
git clone <your-repo-url>
cd eventflow_backend
python -m venv venv

# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate
```

### 2. Run the Uvicorn Server
```bash
uvicorn agent_main:app --reload
```
Open your browser to `http://127.0.0.1:8000/` to access the interactive organizer dashboard.

---

## 🧠 Local Open-Source Models (via Ollama)

To run the EventFlow agentic layer completely offline or to avoid Gemini API quota limit restrictions:

1. **Install Ollama**: Download and install Ollama from [https://ollama.com](https://ollama.com).
2. **Download a Model**: Run your preferred model (e.g. Llama 3) locally:
   ```bash
   ollama run llama3
   ```
3. **Configure Environment Variables**: Set `USE_OLLAMA=true` before starting the server:
   ```bash
   # Unix/macOS:
   export USE_OLLAMA=true
   export OLLAMA_MODEL=llama3  # optional: defaults to llama3
   export OLLAMA_BASE_URL=http://localhost:11434  # optional: defaults to http://localhost:11434
   
   # Windows (CMD):
   set USE_OLLAMA=true
   
   # Windows (PowerShell):
   $env:USE_OLLAMA="true"
   ```
4. **Start Server**: Run `uvicorn agent_main:app --reload`. The backend will automatically direct all AI queries (team rationales, score anomaly explanations, conversational chat) to your local Ollama model.
5. **Robust Fallback**: If the local Ollama service is not running or becomes unreachable, the server will automatically fallback to the built-in deterministic Mock LLM responses to ensure zero-crash operations.