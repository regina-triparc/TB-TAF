"""
External systems, one folder each. All are optional: with no credentials the
service still runs (Jira -> seed file, GitHub -> in-place mode, Paperclip -> log only).

    jira/        board.py (queue for the dashboard), xray.py (test import payloads)
    github/      repository.py (the Playwright test repo: git, install, run)
    paperclip/   control_room.py (run mirror, cost ledger, budget stop)
"""
