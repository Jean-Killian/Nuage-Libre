from fastapi import FastAPI

app = FastAPI(title="Nuage Libre API")

@app.get("/")
def lire_racine():
    return {
        "statut": "Online",
        "projet": "Nuage Libre",
        "message": "FastAPI Backend is working!"
    }