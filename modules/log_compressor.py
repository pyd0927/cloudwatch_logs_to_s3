import gzip
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any
from io import BytesIO


class LogCompressor:
    """로그 데이터를 압축하는 클래스"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def compress_logs(self, logs: List[Dict[str, Any]], format_type: str = 'json', filename: str = None) -> bytes:
        """로그 데이터를 압축합니다."""
        try:
            if not logs:
                self.logger.warning("압축할 로그가 없습니다.")
                # 빈 로그라도 유효한 gzip 파일 생성
                return self._create_empty_gzip()

            if format_type == 'json':
                return self._compress_json(logs, filename)
            elif format_type == 'text':
                return self._compress_text(logs, filename)
            else:
                raise ValueError(f"지원하지 않는 형식: {format_type}")

        except Exception as e:
            self.logger.error(f"로그 압축 실패: {e}")
            raise

    def _create_empty_gzip(self) -> bytes:
        """빈 gzip 파일을 생성합니다."""
        compressed_data = BytesIO()
        with gzip.GzipFile(
            fileobj=compressed_data,
            mode='wb',
            mtime=int(datetime.now(timezone.utc).timestamp())
        ) as gz_file:
            gz_file.write(b'')  # 빈 내용
        return compressed_data.getvalue()

    def _compress_json(self, logs: List[Dict[str, Any]], filename: str = None) -> bytes:
        """JSON 형식으로 로그를 압축합니다."""
        if not logs:
            self.logger.warning("압축할 로그가 없습니다.")
            return self._create_empty_gzip()

        # 고유한 임시 파일명 생성 (동시 실행 방지)
        temp_id = str(uuid.uuid4())[:8]
        temp_json_file = None
        temp_gzip_file = None

        try:
            # 임시 JSON 파일 생성
            temp_json_file = f"/tmp/temp_logs_{temp_id}.json"

            # JSON 파일에 로그 데이터 작성
            with open(temp_json_file, 'w', encoding='utf-8') as json_file:
                for log in logs:
                    try:
                        # timestamp를 ISO 8601 형식으로 변환
                        log_copy = log.copy()
                        timestamp_ms = log_copy.get('timestamp', 0)
                        if timestamp_ms:
                            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                            log_copy['timestamp'] = timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

                        # 로그 데이터를 JSON 문자열로 변환
                        log_line = json.dumps(log_copy, ensure_ascii=False, default=str, separators=(',', ':'))
                        json_file.write(log_line + '\n')
                    except (TypeError, ValueError) as e:
                        self.logger.warning(f"JSON 직렬화 실패, 스킵: {e}")
                        continue

            # JSON 파일 크기 확인
            json_file_size = os.path.getsize(temp_json_file)
            if json_file_size == 0:
                self.logger.warning("생성된 JSON 파일이 비어있습니다.")
                return self._create_empty_gzip()

            # 임시 gzip 파일 생성
            temp_gzip_file = f"/tmp/temp_logs_{temp_id}.gz"

            # gzip 파일명 설정 (압축 해제 시 .json 확장자 자동 추가)
            gzip_filename = None
            if filename:
                # .log.gz 확장자 제거하고 .json 추가
                base_name = filename.replace('.log.gz', '').replace('.gz', '')
                gzip_filename = f"{base_name}.json"

            # JSON 파일을 gzip으로 압축
            with open(temp_json_file, 'rb') as json_in:
                with gzip.GzipFile(filename=gzip_filename, mode='wb', fileobj=open(temp_gzip_file, 'wb')) as gz_file:
                    gz_file.writelines(json_in)

            # 압축된 파일 읽기
            with open(temp_gzip_file, 'rb') as gz_in:
                compressed_bytes = gz_in.read()

            # 압축 검증
            if len(compressed_bytes) > 0:
                # gzip 헤더 확인
                if not compressed_bytes.startswith(b'\x1f\x8b'):
                    self.logger.error("압축된 데이터에 gzip 헤더가 없습니다!")
                    raise ValueError("gzip 압축 실패")

                self.logger.debug(
                    f"원본 크기: {json_file_size} bytes, 압축 비율: "
                    f"{self.get_compression_ratio(json_file_size, len(compressed_bytes)):.1f}% (gzip 헤더 확인됨)")
            else:
                self.logger.warning("압축된 데이터가 비어있습니다.")
                compressed_bytes = self._create_empty_gzip()

            return compressed_bytes

        except Exception as e:
            self.logger.error(f"JSON 압축 실패: {e}")
            raise

        finally:
            # 임시 파일 정리
            try:
                if temp_json_file and os.path.exists(temp_json_file):
                    os.remove(temp_json_file)

                if temp_gzip_file and os.path.exists(temp_gzip_file):
                    os.remove(temp_gzip_file)

            except Exception as e:
                self.logger.warning(f"임시 파일 정리 실패: {e}")

    def _compress_text(self, logs: List[Dict[str, Any]], filename: str = None) -> bytes:
        """텍스트 형식으로 로그를 압축합니다."""
        if not logs:
            self.logger.warning("압축할 로그가 없습니다.")
            return self._create_empty_gzip()

        # 고유한 임시 파일명 생성 (동시 실행 방지)
        temp_id = str(uuid.uuid4())[:8]
        temp_txt_file = None
        temp_gzip_file = None

        try:
            # 임시 텍스트 파일 생성
            temp_txt_file = f"/tmp/temp_logs_{temp_id}.txt"

            # 텍스트 파일에 로그 데이터 작성
            with open(temp_txt_file, 'w', encoding='utf-8') as txt_file:
                for log in logs:
                    try:
                        # 타임스탬프를 ISO 8601 형식으로 변환 (UTC timezone 포함)
                        timestamp_ms = log.get('timestamp', 0)
                        if timestamp_ms:
                            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                            timestamp_str = timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                        else:
                            timestamp_str = 'Unknown'

                        # 로그 메시지 구성
                        message = log.get('message', 'No message')
                        log_stream = log.get('logStream', 'unknown')
                        log_line = f"[{timestamp_str}] [{log_stream}] {message}\n"

                        txt_file.write(log_line)
                    except Exception as e:
                        self.logger.warning(f"텍스트 변환 실패, 스킵: {e}")
                        continue

            # 텍스트 파일 크기 확인
            txt_file_size = os.path.getsize(temp_txt_file)
            if txt_file_size == 0:
                self.logger.warning("생성된 텍스트 파일이 비어있습니다.")
                return self._create_empty_gzip()

            # 임시 gzip 파일 생성
            temp_gzip_file = f"/tmp/temp_logs_{temp_id}.gz"

            # gzip 파일명 설정 (압축 해제 시 .txt 확장자 자동 추가)
            gzip_filename = None
            if filename:
                # .log.gz 확장자 제거하고 .txt 추가
                base_name = filename.replace('.log.gz', '').replace('.gz', '')
                gzip_filename = f"{base_name}.txt"

            # 텍스트 파일을 gzip으로 압축
            with open(temp_txt_file, 'rb') as txt_in:
                with gzip.GzipFile(filename=gzip_filename, mode='wb', fileobj=open(temp_gzip_file, 'wb')) as gz_file:
                    gz_file.writelines(txt_in)

            # 압축된 파일 읽기
            with open(temp_gzip_file, 'rb') as gz_in:
                compressed_bytes = gz_in.read()

            # 압축 검증
            if len(compressed_bytes) > 0:
                # gzip 헤더 확인
                if not compressed_bytes.startswith(b'\x1f\x8b'):
                    self.logger.error("압축된 데이터에 gzip 헤더가 없습니다!")
                    raise ValueError("gzip 압축 실패")

                self.logger.debug(
                    f"원본 크기: {txt_file_size} bytes, 압축 비율: "
                    f"{self.get_compression_ratio(txt_file_size, len(compressed_bytes)):.1f}% (gzip 헤더 확인됨)")
            else:
                self.logger.warning("압축된 데이터가 비어있습니다.")
                compressed_bytes = self._create_empty_gzip()

            return compressed_bytes

        except Exception as e:
            self.logger.error(f"텍스트 압축 실패: {e}")
            raise

        finally:
            # 임시 파일 정리
            try:
                if temp_txt_file and os.path.exists(temp_txt_file):
                    os.remove(temp_txt_file)

                if temp_gzip_file and os.path.exists(temp_gzip_file):
                    os.remove(temp_gzip_file)

            except Exception as e:
                self.logger.warning(f"임시 파일 정리 실패: {e}")

    def get_compression_ratio(self, original_size: int, compressed_size: int) -> float:
        """압축 비율을 계산합니다."""
        if original_size == 0:
            return 0.0
        return (1 - compressed_size / original_size) * 100
