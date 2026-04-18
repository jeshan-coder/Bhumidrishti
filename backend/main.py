from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="BhumiDrishti API",
    description="Offline-first disaster damage assessment platform API",
    version="1.0.0"
)

# CORS enabled for localhost:3000
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "success": True,
        "data": {"message": "Hello from BhumiDrishti Backend"},
        "error": None
    }
