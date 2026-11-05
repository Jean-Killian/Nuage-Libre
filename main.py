from pathlib import Path
import json
import urllib.request
import urllib.error
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="Nuage Libre API")

BASE_DIR = Path(__file__).resolve().parent
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL_NAME = "qwen2.5:14b"


class ChatRequest(BaseModel):
    message: str | None = None
    prompt: str | None = None

@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")


@app.post("/chat")
def chat_with_ollama(payload: ChatRequest):
    user_message = (payload.message or payload.prompt or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    ollama_payload = json.dumps({
        "model": MODEL_NAME,
        "prompt": user_message,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=ollama_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            ai_response = data.get("response", "")
            return {"response": ai_response}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        raise HTTPException(
            status_code=e.code,
            detail=f"Ollama HTTP error: {error_body or e.reason}",
        )
    except urllib.error.URLError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to local Ollama instance at {OLLAMA_URL}: {e.reason}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
