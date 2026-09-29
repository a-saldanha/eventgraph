"""Per-file ingest status report, stored in the bundle and returned by job polling."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileResult:
    name: str
    detected_format: str
    status: str          # ok | partial | failed | skipped
    items: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "detected_format": self.detected_format,
            "status": self.status,
            "items": self.items,
            "warnings": self.warnings,
            "error": self.error,
        }


@dataclass
class IngestReport:
    files: list[FileResult] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return sum(1 for f in self.files if f.status in ("ok", "partial"))

    @property
    def n_failed(self) -> int:
        return sum(1 for f in self.files if f.status == "failed")

    @property
    def n_skipped(self) -> int:
        return sum(1 for f in self.files if f.status == "skipped")

    def to_dict(self) -> dict:
        return {
            "files": [f.to_dict() for f in self.files],
            "total_ok": self.n_ok,
            "total_failed": self.n_failed,
            "total_skipped": self.n_skipped,
        }
