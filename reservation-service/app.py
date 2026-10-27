"""
Entry point for the Resource Reservation Service.

Run:  uvicorn app:app --reload

The implementation lives in the `reservations` package; see docs/ARCHITECTURE.md.
"""

from reservations import create_app

app = create_app()
