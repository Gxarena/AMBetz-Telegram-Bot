# app/main.py

from fastapi import FastAPI
from app.routes import router as paypal_router

# Optional: If using a scheduler for routine checks
from app.scheduler import start_scheduler

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    # If you want a routine subscription check, you can start a scheduler here.
    start_scheduler()

@app.get("/")
def read_root():
    return {"message": "Subscription service is running!"}

# Include PayPal routes with a prefix for clarity
app.include_router(paypal_router, prefix="/paypal")
