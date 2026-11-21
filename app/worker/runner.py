"""워커 진입점: `python -m app.worker.runner`

별도 브로커 없이 Postgres jobs 테이블을 폴링한다 (SELECT ... FOR UPDATE SKIP LOCKED).
"""

import logging
import signal
import time

from app.config import get_settings
from app.db import get_sessionmaker
from app.integrations import get_integrations
from app.logging import configure_logging
from app.worker.pipeline import JobRunner, claim_job, reap_stale_jobs
from app.worker.scheduler import publish_due_episodes

logger = logging.getLogger("app.worker")


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.session_factory = get_sessionmaker()
        self.runner = JobRunner(self.session_factory, get_integrations())
        self.stopping = False
        self._last_schedule = 0.0
        self._last_reap = 0.0

    def run_once(self) -> bool:
        """주기 작업 후 대기 작업 하나를 실행한다. 실행했으면 True."""
        now = time.monotonic()
        if now - self._last_reap >= 10:
            with self.session_factory() as s:
                reap_stale_jobs(
                    s, self.settings.job_heartbeat_timeout_seconds, self.settings.job_max_attempts
                )
            self._last_reap = now
        if now - self._last_schedule >= self.settings.scheduler_interval_seconds:
            with self.session_factory() as s:
                publish_due_episodes(s)
            self._last_schedule = now
        with self.session_factory() as s:
            job_id = claim_job(s)
        if job_id is None:
            return False
        logger.info("job started", extra={"job_id": job_id})
        self.runner.run(job_id)
        logger.info("job finished", extra={"job_id": job_id})
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        logger.info("worker started")
        while not self.stopping:
            try:
                if not self.run_once():
                    time.sleep(self.settings.worker_poll_seconds)
            except Exception:
                logger.exception("worker loop error")
                time.sleep(self.settings.worker_poll_seconds)
        logger.info("worker stopped")

    def _stop(self, *_: object) -> None:
        self.stopping = True


def main() -> None:
    configure_logging(get_settings().log_level)
    Worker().run_forever()


if __name__ == "__main__":
    main()
