from abc import ABC, abstractmethod
from uuid import UUID

class JobRunner(ABC):
    @abstractmethod
    def submit_job(self, job_id: UUID, **kwargs) -> None:
        """Mendaftarkan ingest job ke eksekutor asynchronous."""
        pass
