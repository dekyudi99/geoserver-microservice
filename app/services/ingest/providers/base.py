from abc import ABC, abstractmethod
from pathlib import Path
from typing import NamedTuple, Optional

class DownloadedFile(NamedTuple):
    local_path: Path
    original_filename: str
    file_size: int
    content_type: Optional[str] = None

class SourceProvider(ABC):
    @abstractmethod
    def download(self, target_dir: Path) -> DownloadedFile:
        """Mengunduh file dari sumber ke disk staging lokal."""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Membersihkan file staging sementara setelah ingest selesai/gagal."""
        pass
