"""NAPlatform department Hermes agent HTTP service (Phase 07).

A small FastAPI service that each department Hermes agent container runs. It
exposes ``/health``, ``/chat`` and ``/invoke``, validates the API-issued
``AgentContext`` scope carried in every request, and returns a deterministic
response (``hermes_invoked=false``) unless real Hermes CLI execution is
explicitly enabled. No live Hermes CLI is required for tests.
"""
