"""Jobs admin API — the queue was previously invisible outside sqlite3.

    GET    /api/jobs                 ?state=&type=&limit=   list jobs
    GET    /api/jobs/stats           counts per state + errors by type
    POST   /api/jobs/{id}/retry      requeue one error/parked/cancelled job
    POST   /api/jobs/retry-errors    ?type=                 requeue all errors
    POST   /api/jobs/{id}/cancel     cancel a queued/parked job
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from . import service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(state: Optional[str] = None, type: Optional[str] = None, limit: int = 100):
    return service.list_jobs(state=state, job_type=type, limit=limit)


@router.get("/stats")
def stats():
    return service.job_stats()


@router.post("/retry-errors")
def retry_errors(type: Optional[str] = None):
    n = service.retry_errors(job_type=type)
    return {"requeued": n}


@router.post("/{job_id}/retry")
def retry(job_id: int):
    if not service.retry_job(job_id):
        raise HTTPException(status_code=404, detail="job not found or not retryable")
    return {"ok": True}


@router.post("/{job_id}/cancel")
def cancel(job_id: int):
    if not service.cancel_job(job_id):
        raise HTTPException(status_code=404, detail="job not found or not cancellable")
    return {"ok": True}
