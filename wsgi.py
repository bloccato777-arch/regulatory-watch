"""WSGI entry point with scheduler initialization."""
from app import app, seed_initial_data, start_scheduler

seed_initial_data()
start_scheduler()
