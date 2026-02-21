import asyncio
import logging
import uuid

from backend.app.db import db
from backend.app.integrations.telegram_bot import telegram_bot_client
from backend.app.reminders import run_inactivity_2d_reminders


logger = logging.getLogger("fitai-inactivity-2d-script")


async def _run() -> int:
    job_run_id = str(uuid.uuid4())
    await db.create_pool()
    if db.pool is None:
        logger.error("INACTIVITY_2D_JOB_ABORT job_run_id=%s reason=no_db_pool", job_run_id)
        return 1

    try:
        async with db.pool.acquire() as conn:
            stats = await run_inactivity_2d_reminders(
                conn,
                sender=telegram_bot_client.send_message,
                job_run_id=job_run_id,
            )
            logger.info(
                "INACTIVITY_2D_JOB_SUMMARY job_run_id=%s total_scanned=%s eligible=%s sent=%s skipped=%s failed=%s",
                job_run_id,
                stats.total_scanned,
                stats.eligible,
                stats.sent,
                stats.skipped,
                stats.failed,
            )
            return 0
    finally:
        await db.close_pool()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    exit_code = asyncio.run(_run())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
