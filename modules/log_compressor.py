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

                        # CloudWatch Log 구조 파싱 및 개선
                        log_copy = self._parse_cloudwatch_log(log_copy)

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
                        # CloudWatch Log 구조 파싱 및 개선
                        enhanced_log = self._parse_cloudwatch_log(log)

                        # 타임스탬프를 ISO 8601 형식으로 변환 (UTC timezone 포함)
                        timestamp_ms = enhanced_log.get('timestamp', 0)
                        if timestamp_ms:
                            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                            timestamp_str = timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                        else:
                            timestamp_str = 'Unknown'

                        # 로그 메시지 구성
                        log_stream = enhanced_log.get('logStream', 'unknown')

                        # CloudWatch Log 구조인 경우 개선된 형태로 출력
                        if 'message' in enhanced_log and isinstance(enhanced_log['message'], dict):
                            cloudwatch_data = enhanced_log['message']
                            log_content = cloudwatch_data.get('log', 'No message')

                            # log 내용이 JSON 객체인 경우 예쁘게 포맷팅
                            if isinstance(log_content, dict):
                                log_content_str = json.dumps(log_content, ensure_ascii=False, indent=2)
                            else:
                                log_content_str = str(log_content)

                            log_line = f"[{timestamp_str}] [{log_stream}] {log_content_str}\n"
                        else:
                            # 기존 방식
                            message = enhanced_log.get('message', 'No message')
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

    def _parse_cloudwatch_log(self, log_entry: Dict[str, Any]) -> Dict[str, Any]:
        """CloudWatch Log 구조를 파싱하여 개선된 형태로 변환합니다."""
        try:
            # message 필드가 있는지 확인
            message = log_entry.get('message', '')
            if not message:
                return log_entry

            # message가 JSON 문자열인지 확인
            try:
                cloudwatch_data = json.loads(message)

                # CloudWatch Log 구조인지 확인 (time, stream, log 필드 존재)
                if isinstance(cloudwatch_data, dict) and 'log' in cloudwatch_data:
                    # log 필드의 내용을 파싱
                    log_content = cloudwatch_data.get('log', '')

                    # log 내용이 JSON인지 확인
                    parsed_log_content = self._parse_log_content(log_content)

                    # CloudWatch Log 구조를 그대로 유지하면서 log 필드만 파싱된 내용으로 교체
                    cloudwatch_data['log'] = parsed_log_content

                    # 원본 log_entry의 기본 필드들과 CloudWatch 데이터를 결합
                    enhanced_log = {
                        'timestamp': log_entry.get('timestamp'),
                        'ingestionTime': log_entry.get('ingestionTime'),
                        'logStream': log_entry.get('logStream'),
                        'message': cloudwatch_data  # CloudWatch Log 구조를 그대로 유지
                    }

                    return enhanced_log

            except (json.JSONDecodeError, TypeError):
                # message가 JSON이 아닌 경우 원본 유지
                pass

        except Exception as e:
            self.logger.warning(f"CloudWatch Log 파싱 실패: {e}")

        return log_entry

    def _parse_log_content(self, log_content: str) -> Any:
        """로그 내용을 파싱하여 JSON이면 객체로, 아니면 문자열로 반환합니다."""
        if not log_content or not isinstance(log_content, str):
            return log_content

        # JSON 형태인지 확인
        try:
            # JSON 파싱 시도
            parsed = json.loads(log_content)
            return parsed
        except (json.JSONDecodeError, TypeError):
            # JSON이 아닌 경우 원본 문자열 반환
            return log_content

    def get_compression_ratio(self, original_size: int, compressed_size: int) -> float:
        """압축 비율을 계산합니다."""
        if original_size == 0:
            return 0.0
        return (1 - compressed_size / original_size) * 100
