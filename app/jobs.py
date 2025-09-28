import asyncio
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    job_id: str
    status: str = "queued"  # queued|running|succeeded|failed
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    summary: str = ""
    result: Dict[str, Any] = field(default_factory=dict)


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> Job:
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id)
        async with self._lock:
            self._jobs[job_id] = job
        return job

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **updates: Any) -> Optional[Job]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for k, v in updates.items():
                setattr(job, k, v)
            return job

    async def serialize(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = await self.get(job_id)
        if not job:
            return None
        data = asdict(job)
        # Provide a result URL for convenience
        data["job_id"] = job.job_id
        data["result_url"] = f"/jobs/{job.job_id}/result"
        return data

