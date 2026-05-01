"""Test session bootstrap. Runs before any test module is imported.

server.py reads ADMIN_EMAILS from env at module load — set a test value
here so admin-gated endpoints have a known admin identity in tests.
"""
import os

os.environ.setdefault("ADMIN_EMAILS", "admin@test.local")
