"""Test session bootstrap. Runs before any test module is imported.

server.py reads LEGACY_SESSION from env at module load — set a test value
here so the import doesn't blow up. Each test that needs the legacy session
uses this same value via the LEGACY_SESSION fixture.
"""
import os

os.environ.setdefault("LEGACY_SESSION", "test_legacy_secret")
