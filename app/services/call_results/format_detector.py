"""Detect call result file format from headers and filename."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.call_results.adapters.batch_filename import BatchFilenameMeta, parse_batch_filename
from app.services.call_results.adapters.tomoru import TomoruCallResultAdapter


@dataclass
class FormatDetectionResult:
    source_format: str  # tomoru_csv | generic
    is_tomoru: bool
    auto_mapping: dict[str, str] | None
    batch_meta: BatchFilenameMeta


class FormatDetector:
    @staticmethod
    def detect(headers: list[str], filename: str) -> FormatDetectionResult:
        batch_meta = parse_batch_filename(filename)
        if TomoruCallResultAdapter.is_tomoru_format(headers):
            return FormatDetectionResult(
                source_format="tomoru_csv",
                is_tomoru=True,
                auto_mapping=TomoruCallResultAdapter.auto_mapping(headers),
                batch_meta=batch_meta,
            )
        return FormatDetectionResult(
            source_format="generic",
            is_tomoru=False,
            auto_mapping=None,
            batch_meta=batch_meta,
        )
