import threading
import logging
from uuid import UUID
from typing import Dict, Optional, Any
from fastapi import BackgroundTasks
from sqlalchemy.sql import func

from app.services.ingest.runners.base import JobRunner
from app.config.settings import settings
from app.config.database import SessionLocal
from app.models.ingest_job import IngestJob, IngestStatusEnum
from app.services.ingest.processor import IngestProcessor

logger = logging.getLogger("geoserver_service.ingest_runner")

_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(settings.MAX_TOTAL_CONCURRENT_JOBS)
_OWNER_SEMAPHORES: Dict[UUID, threading.BoundedSemaphore] = {}
_SEMAPHORE_LOCK = threading.Lock()

def get_owner_semaphore(owner_id: UUID) -> threading.BoundedSemaphore:
    with _SEMAPHORE_LOCK:
        if owner_id not in _OWNER_SEMAPHORES:
            _OWNER_SEMAPHORES[owner_id] = threading.BoundedSemaphore(settings.MAX_CONCURRENT_JOBS_PER_OWNER)
        return _OWNER_SEMAPHORES[owner_id]

def _execute_job_sync(job_id_str: str, download_payload: Optional[Dict[str, Any]] = None):
    """Pekerja sinkron di background thread untuk memproses berkas staging geospasial."""
    db = SessionLocal()
    owner_sem = None
    global_sem_acquired = False
    owner_sem_acquired = False

    try:
        job: IngestJob = db.query(IngestJob).filter(IngestJob.id == job_id_str).first()
        if not job:
            logger.error(f"Job {job_id_str} not found in database.")
            return

        owner_sem = get_owner_semaphore(job.owner_id)

        # Acquire semaphores
        _GLOBAL_SEMAPHORE.acquire()
        global_sem_acquired = True
        owner_sem.acquire()
        owner_sem_acquired = True

        # Transition ke RUNNING
        job.status = IngestStatusEnum.RUNNING
        job.started_at = func.now()
        job.progress = 10
        db.commit()

        # Eksekusi Core Processing Pipeline
        result = IngestProcessor.process(job_id_str, db)

        # Transition ke COMPLETED
        job.status = IngestStatusEnum.COMPLETED
        job.progress = 100
        job.result_layer_name = result.get("table_name") or result.get("layer_name")
        job.result_metadata = result
        job.finished_at = func.now()
        db.commit()
        logger.info(f"Ingest Job {job_id_str} COMPLETED successfully.")

    except Exception as e:
        logger.exception(f"Ingest Job {job_id_str} FAILED: {e}")
        try:
            db.rollback()
            failed_job = db.query(IngestJob).filter(IngestJob.id == job_id_str).first()
            if failed_job:
                failed_job.status = IngestStatusEnum.FAILED
                failed_job.error_message = str(e)
                failed_job.finished_at = func.now()
                db.commit()
        except Exception as db_err:
            logger.error(f"Could not persist failed status for job {job_id_str}: {db_err}")
    finally:
        if owner_sem_acquired and owner_sem:
            owner_sem.release()
        if global_sem_acquired:
            _GLOBAL_SEMAPHORE.release()
        db.close()

class BackgroundTasksRunner(JobRunner):
    def __init__(self, background_tasks: BackgroundTasks):
        self.background_tasks = background_tasks

    def submit_job(self, job_id: UUID, download_payload: Optional[Dict[str, Any]] = None, **kwargs) -> None:
        self.background_tasks.add_task(_execute_job_sync, str(job_id), download_payload)
        logger.info(f"Submitted Ingest Job {job_id} to BackgroundTasks.")
