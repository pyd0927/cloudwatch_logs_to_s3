import json
import boto3
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from botocore.exceptions import ClientError, NoCredentialsError
from .config import Config


class S3StateManager:
    """S3에 로그 읽기 상태를 저장하는 클래스"""

    def __init__(self, bucket_name: str = None, state_prefix: str = None):
        self.bucket_name = bucket_name or Config.S3_BUCKET_NAME
        self.state_prefix = state_prefix or Config.S3_STATE_PREFIX
        self.logger = logging.getLogger(__name__)
        self.s3_client = boto3.client('s3', region_name=Config.REGION)

        # 상태 캐시 (메모리에서 중복 S3 호출 방지) - 로그그룹별로 관리
        self._state_cache = {}  # {log_group_name: state_data}
        self._cache_timestamp = {}  # {log_group_name: timestamp}
        self._cache_ttl_seconds = 60  # 1분 캐시

        # State 파일 접근용 Lock (로그그룹별) - 멀티스레딩 경쟁 조건 방지
        self._state_locks = {}  # {log_group_name: Lock}
        self._locks_lock = threading.Lock()  # Lock 생성용 Lock

        # 마이그레이션 실행 (한 번만)
        self._migrate_legacy_state()

    def _get_state_key_for_log_group(self, log_group_name: str) -> str:
        """로그그룹별 State 파일 키를 생성합니다."""
        # 로그그룹명에서 '/' 제거하여 S3 키 생성
        clean_name = log_group_name.replace('/', '_')
        return f"{self.state_prefix}/{clean_name}/state.json"

    def _get_lock_for_log_group(self, log_group_name: str) -> threading.Lock:
        """로그그룹별 Lock을 가져오거나 생성합니다."""
        with self._locks_lock:
            if log_group_name not in self._state_locks:
                self._state_locks[log_group_name] = threading.Lock()
            return self._state_locks[log_group_name]

    def _is_cache_valid(self, log_group_name: str) -> bool:
        """특정 로그그룹의 캐시가 유효한지 확인합니다."""
        if log_group_name not in self._cache_timestamp:
            return False

        cache_age = (datetime.now(timezone.utc) - self._cache_timestamp[log_group_name]).total_seconds()
        return cache_age < self._cache_ttl_seconds

    def _load_from_s3(self, log_group_name: str) -> Dict[str, Any]:
        """S3에서 특정 로그그룹의 상태를 로드합니다."""
        state_key = self._get_state_key_for_log_group(log_group_name)

        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=state_key
            )

            content = response['Body'].read().decode('utf-8')
            state_data = json.loads(content)

            # 캐시 업데이트
            self._state_cache[log_group_name] = state_data
            self._cache_timestamp[log_group_name] = datetime.now(timezone.utc)

            self.logger.info(f"S3 상태 로드 완료(s3://{self.bucket_name}/{state_key})")
            return state_data

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'NoSuchKey':
                self.logger.info(f"S3 상태 파일이 존재하지 않음: {log_group_name}. 새로 생성합니다.")
                return {}
            elif error_code == 'NoSuchBucket':
                self.logger.error(f"S3 버킷이 존재하지 않습니다: {self.bucket_name}")
                raise ValueError(f"S3 버킷 '{self.bucket_name}'이 존재하지 않습니다.")
            else:
                self.logger.error(f"S3 상태 로드 실패: {e}")
                return {}
        except json.JSONDecodeError as e:
            self.logger.error(f"상태 파일 JSON 파싱 실패: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"예상치 못한 S3 로드 오류: {e}")
            return {}

    def _save_to_s3(self, log_group_name: str, state_data: Dict[str, Any]) -> bool:
        """S3에 특정 로그그룹의 상태를 저장합니다."""
        state_key = self._get_state_key_for_log_group(log_group_name)

        try:
            # 메타데이터 추가
            metadata = {
                'last_updated': datetime.now(timezone.utc).isoformat(),
                'log_group_name': log_group_name,
                'manager_version': '3.0'
            }

            json_content = json.dumps(
                state_data,
                indent=2,
                ensure_ascii=False,
                default=str
            )

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=state_key,
                Body=json_content.encode('utf-8'),
                ContentType='application/json',
                ServerSideEncryption='AES256',
                Metadata=metadata
            )

            # 캐시 업데이트
            self._state_cache[log_group_name] = state_data
            self._cache_timestamp[log_group_name] = datetime.now(timezone.utc)

            self.logger.info(f"S3 상태 저장 완료: {log_group_name}")
            return True

        except ClientError as e:
            self.logger.error(f"S3 상태 저장 실패: {e}")
            return False
        except Exception as e:
            self.logger.error(f"예상치 못한 S3 저장 오류: {e}")
            return False

    def _migrate_legacy_state(self) -> bool:
        """기존 통합 State 파일을 개별 파일로 마이그레이션합니다."""
        legacy_state_key = f"{self.state_prefix}/state.json"

        try:
            # 기존 통합 State 파일 존재 확인
            self.s3_client.head_object(Bucket=self.bucket_name, Key=legacy_state_key)
            self.logger.info("기존 통합 State 파일 발견. 마이그레이션을 시작합니다.")

            # 기존 파일 로드
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=legacy_state_key)
            content = response['Body'].read().decode('utf-8')
            legacy_data = json.loads(content)

            if not isinstance(legacy_data, dict):
                self.logger.warning("기존 State 파일 형식이 올바르지 않습니다. 마이그레이션을 건너뜁니다.")
                return False

            # 각 로그그룹별로 개별 파일 생성
            migrated_count = 0
            for log_group_name, state_data in legacy_data.items():
                if isinstance(state_data, dict) and 'log_group_name' in state_data:
                    success = self._save_to_s3(log_group_name, state_data)
                    if success:
                        migrated_count += 1
                        self.logger.info(f"마이그레이션 완료: {log_group_name}")
                    else:
                        self.logger.error(f"마이그레이션 실패: {log_group_name}")

            if migrated_count > 0:
                # 기존 파일 삭제
                self.s3_client.delete_object(Bucket=self.bucket_name, Key=legacy_state_key)
                self.logger.info(f"마이그레이션 완료: {migrated_count}개 로그그룹, 기존 파일 삭제됨")
                return True
            else:
                self.logger.warning("마이그레이션할 유효한 데이터가 없습니다.")
                return False

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code in ['NoSuchKey', '404']:
                # 기존 파일이 없으면 마이그레이션 불필요 (정상 상황)
                self.logger.warning("기존 통합 State 파일이 없습니다. 마이그레이션을 건너뜁니다.")
                return True
            else:
                self.logger.error(f"마이그레이션 중 오류 발생: {e}")
                return False
        except Exception as e:
            # 404 오류가 ClientError로 잡히지 않는 경우를 대비
            error_str = str(e)
            if '404' in error_str or 'Not Found' in error_str:
                # 기존 파일이 없으면 마이그레이션 불필요 (정상 상황)
                self.logger.warning("기존 통합 State 파일이 없습니다. 마이그레이션을 건너뜁니다.")
                return True
            else:
                self.logger.error(f"마이그레이션 중 예상치 못한 오류: {e}")
                return False

    def load_state(self, log_group_name: str) -> Dict[str, Any]:
        """특정 로그그룹의 상태를 로드합니다."""
        try:
            # 캐시에서 먼저 확인
            if self._is_cache_valid(log_group_name) and log_group_name in self._state_cache:
                state_data = self._state_cache[log_group_name]
                self.logger.debug(f"캐시에서 상태 로드: {log_group_name}")
            else:
                # S3에서 로드
                state_data = self._load_from_s3(log_group_name)

            # 상태가 비어있으면 기본 상태 반환
            if not state_data:
                return self._get_default_state(log_group_name)

            return state_data

        except Exception as e:
            self.logger.error(f"상태 로드 실패: {e}")
            return self._get_default_state(log_group_name)

    def save_state(self, log_group_name: str, state: Dict[str, Any]) -> bool:
        """특정 로그그룹의 상태를 저장합니다."""
        try:
            # S3에 저장
            success = self._save_to_s3(log_group_name, state)

            if success:
                self.logger.info(f"상태 저장 완료: {log_group_name}")

            return success

        except Exception as e:
            self.logger.error(f"상태 저장 실패: {e}")
            return False

    def update_stream_state(
        self,
        log_group_name: str,
        stream_name: str,
        next_token: str,
        last_event_time: int,
    ) -> bool:
        """특정 스트림의 상태를 업데이트합니다."""
        lock = self._get_lock_for_log_group(log_group_name)

        with lock:  # 멀티스레딩 경쟁 조건 방지
            try:
                state = self.load_state(log_group_name)

                # 스트림 상태 업데이트
                state['streams'][stream_name] = {
                    'next_token': next_token,
                    'last_event_time': last_event_time,
                    'last_updated': datetime.now(timezone.utc).isoformat()
                }

                return self.save_state(log_group_name, state)

            except Exception as e:
                self.logger.error(f"스트림 상태 업데이트 실패: {e}")
                return False

    def get_stream_state(self, log_group_name: str, stream_name: str) -> Optional[Dict[str, Any]]:
        """특정 스트림의 상태를 가져옵니다."""
        try:
            state = self.load_state(log_group_name)
            return state['streams'].get(stream_name)

        except Exception as e:
            self.logger.error(f"스트림 상태 로드 실패: {e}")
            return None

    def get_last_read_time(self, log_group_name: str) -> Optional[datetime]:
        """로그그룹의 마지막 읽은 시간을 가져옵니다."""
        try:
            state = self.load_state(log_group_name)

            # last_read_time이 있으면 우선 사용 (마지막으로 로그를 읽은 시간)
            last_read_time = state.get('last_read_time')
            if last_read_time:
                return datetime.fromisoformat(last_read_time.replace('Z', '+00:00'))

            # last_read_time이 없으면 스트림의 last_event_time 사용 (하위 호환성)
            streams = state.get('streams', {})
            if not streams:
                return None

            # 모든 스트림 중 가장 최근의 last_event_time을 찾기 (실제 마지막 이벤트 시간)
            latest_time = None
            for stream_name, stream_state in streams.items():
                last_event_time = stream_state.get('last_event_time')
                if last_event_time:
                    # Unix timestamp를 datetime으로 변환
                    event_time = datetime.fromtimestamp(last_event_time / 1000, tz=timezone.utc)
                    if latest_time is None or event_time > latest_time:
                        latest_time = event_time

            return latest_time

        except Exception as e:
            self.logger.error(f"마지막 읽은 시간 조회 실패: {e}")
            return None

    def get_last_run_time(self, log_group_name: str) -> Optional[datetime]:
        """로그그룹의 마지막 실행 시간을 가져옵니다."""
        try:
            state = self.load_state(log_group_name)
            last_run_time = state.get('last_run_time')
            if last_run_time:
                return datetime.fromisoformat(last_run_time.replace('Z', '+00:00'))
            return None
        except Exception as e:
            self.logger.error(f"마지막 실행 시간 조회 실패: {e}")
            return None

    def get_streams_last_event_time(self, log_group_name: str) -> Dict[str, datetime]:
        """각 스트림의 마지막 이벤트 시간을 가져옵니다."""
        try:
            state = self.load_state(log_group_name)
            streams = state.get('streams', {})

            stream_times = {}
            for stream_name, stream_state in streams.items():
                last_event_time = stream_state.get('last_event_time')
                if last_event_time:
                    # Unix timestamp를 datetime으로 변환
                    event_time = datetime.fromtimestamp(last_event_time / 1000, tz=timezone.utc)
                    stream_times[stream_name] = event_time

            return stream_times

        except Exception as e:
            self.logger.error(f"스트림별 마지막 이벤트 시간 조회 실패: {e}")
            return {}

    def update_last_run_time(self, log_group_name: str) -> bool:
        """로그그룹의 마지막 실행 시간을 업데이트합니다."""
        lock = self._get_lock_for_log_group(log_group_name)

        with lock:  # 멀티스레딩 경쟁 조건 방지
            try:
                state = self.load_state(log_group_name)
                state['last_run_time'] = datetime.now(timezone.utc).isoformat()
                return self.save_state(log_group_name, state)
            except Exception as e:
                self.logger.error(f"마지막 실행 시간 업데이트 실패: {e}")
                return False

    def update_last_read_time(self, log_group_name: str, read_time: datetime) -> bool:
        """로그그룹의 마지막 읽은 시간을 업데이트합니다."""
        lock = self._get_lock_for_log_group(log_group_name)

        with lock:  # 멀티스레딩 경쟁 조건 방지
            try:
                state = self.load_state(log_group_name)
                state['last_read_time'] = read_time.isoformat()
                return self.save_state(log_group_name, state)
            except Exception as e:
                self.logger.error(f"마지막 읽은 시간 업데이트 실패: {e}")
                return False

    def _get_default_state(self, log_group_name: str) -> Dict[str, Any]:
        """기본 상태를 반환합니다."""
        return {
            'log_group_name': log_group_name,
            'last_run_time': None,
            'last_read_time': None,
            'streams': {},
            'created_at': datetime.now(timezone.utc).isoformat(),
            'state_version': '2.0'
        }

    def cleanup_old_streams(
        self,
        log_group_name: str,
        active_streams: list,
        max_age_hours: int = 24,
    ) -> bool:
        """오래된 스트림 상태를 정리합니다."""
        lock = self._get_lock_for_log_group(log_group_name)

        with lock:  # 멀티스레딩 경쟁 조건 방지
            try:
                state = self.load_state(log_group_name)
                current_time = datetime.now(timezone.utc)
                cleaned_count = 0

                streams_to_remove = []
                for stream_name, stream_state in state['streams'].items():
                    # 오래된 스트림만 제거 (활성 스트림 여부는 무시)
                    if self._is_stream_old(stream_state, current_time, max_age_hours):
                        streams_to_remove.append(stream_name)

                for stream_name in streams_to_remove:
                    del state['streams'][stream_name]
                    cleaned_count += 1

                if cleaned_count > 0:
                    self.save_state(log_group_name, state)
                    self.logger.info(f"{cleaned_count}개의 오래된 스트림 상태를 정리했습니다.")

                return True

            except Exception as e:
                self.logger.error(f"오래된 스트림 정리 실패: {e}")
                return False

    def _is_stream_old(self, stream_state: Dict[str, Any], current_time: datetime,
                       max_age_hours: int) -> bool:
        """스트림이 오래되었는지 확인합니다."""
        try:
            last_updated_str = stream_state.get('last_updated')
            if not last_updated_str:
                return True

            last_updated = datetime.fromisoformat(last_updated_str.replace('Z', '+00:00'))
            age_hours = (current_time - last_updated).total_seconds() / 3600

            return age_hours > max_age_hours

        except Exception:
            return True

    def get_state_info(self, log_group_name: str) -> Dict[str, Any]:
        """특정 로그그룹의 상태 파일 정보를 반환합니다."""
        state_key = self._get_state_key_for_log_group(log_group_name)

        try:
            # S3 객체 메타데이터 확인
            response = self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=state_key
            )

            return {
                'bucket': self.bucket_name,
                'key': state_key,
                'log_group_name': log_group_name,
                'size': response.get('ContentLength', 0),
                'last_modified': response.get('LastModified'),
                'metadata': response.get('Metadata', {}),
                'cache_valid': self._is_cache_valid(log_group_name),
                'cache_timestamp': self._cache_timestamp.get(log_group_name)
            }

        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                return {
                    'bucket': self.bucket_name,
                    'key': state_key,
                    'log_group_name': log_group_name,
                    'exists': False,
                    'cache_valid': self._is_cache_valid(log_group_name)
                }
            else:
                raise

    def validate_s3_access(self) -> bool:
        """S3 접근 권한을 확인합니다."""
        try:
            # 버킷 존재 확인
            self.s3_client.head_bucket(Bucket=self.bucket_name)

            # State 디렉토리에 테스트 파일 생성/삭제로 쓰기 권한 확인
            test_key = f"{self.state_prefix}/.test_access"
            try:
                # 테스트 파일 생성
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=test_key,
                    Body=b'test',
                    ContentType='text/plain',
                    ServerSideEncryption='AES256'
                )

                # 테스트 파일 삭제
                self.s3_client.delete_object(Bucket=self.bucket_name, Key=test_key)

                self.logger.info(f"S3 접근 권한 확인 완료: {self.bucket_name}")
                return True

            except ClientError as e:
                self.logger.error(f"S3 쓰기 권한 확인 실패: {e}")
                return False

        except ClientError as e:
            self.logger.error(f"S3 접근 권한 확인 실패: {e}")
            return False
        except NoCredentialsError:
            self.logger.error("AWS 자격 증명이 없습니다.")
            return False
        except Exception as e:
            self.logger.error(f"S3 접근 권한 확인 중 오류: {e}")
            return False
