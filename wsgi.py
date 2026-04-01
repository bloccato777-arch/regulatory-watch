"""WSGI entry point — initializes seed data and starts the background scheduler."""
from app import app, seed_initial_data, start_scheduler

seed_initial_data()
start_scheduler()
