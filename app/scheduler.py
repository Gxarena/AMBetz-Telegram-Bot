# app/scheduler.py

from apscheduler.schedulers.background import BackgroundScheduler

def check_subscriptions():
    """
    Query your database for expired or soon-to-expire subscriptions.
    Kick or notify users as needed (interact with your Telegram bot's logic).
    """
    print("Running subscription check...")

def start_scheduler():
    scheduler = BackgroundScheduler()
    # Run checks every hour, for example
    scheduler.add_job(check_subscriptions, 'interval', hours=1)
    scheduler.start()
