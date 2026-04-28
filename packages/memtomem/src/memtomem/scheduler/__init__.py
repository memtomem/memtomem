"""Scheduler — JOB_KINDS registry for periodic memory-lifecycle work.

Phase A of the natural-language-cron RFC (P2). The dispatcher lives in
``server/health_watchdog.py`` (PR-A3); this package only owns the
typed registry of what *can* be scheduled.
"""

from memtomem.scheduler.jobs import JOB_KINDS, JobResult, JobRunStatus, JobSpec

__all__ = ["JOB_KINDS", "JobResult", "JobRunStatus", "JobSpec"]
