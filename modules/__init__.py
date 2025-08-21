"""
CloudWatch Log to S3 모듈 패키지
"""

import logging
from typing import Optional

from .config import Config
from .cloudwatch_log_reader import CloudWatchLogReader
from .log_compressor import LogCompressor
from .s3_uploader import S3Uploader
from .s3_state_manager import S3StateManager
from .batch_processor import BatchProcessor


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """모듈별 로거를 반환합니다."""
    return logging.getLogger(name or __name__)


__all__ = [
    'Config',
    'CloudWatchLogReader',
    'LogCompressor',
    'S3Uploader',
    'S3StateManager',
    'BatchProcessor',
    'get_logger'
]
