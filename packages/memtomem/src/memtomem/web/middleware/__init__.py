"""HTTP middleware for the memtomem Web UI.

Each module here encapsulates one cross-cutting concern. Compose them in
``web.app.create_app`` rather than spreading the wiring across handlers —
that keeps the AST-walking registry in
``tests/test_web_invariants_registry.py`` honest about which routes are
gated by which guard.
"""
