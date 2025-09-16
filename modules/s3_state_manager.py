import json
import boto3
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from botocore.exceptions import ClientError, NoCredentialsError
from .config import Config


class S3StateManager:
    """S3에 로그 읽기 상태를 저장하는 클래스"""

    def __init__(self, bucket_name: str = None, state_key: str = None):
        self.bucket_name = bucket_name or Config.S3_BUCKET_NAME
        self.state_key = state_key or Config.S3_STATE_KEY
        self.logger = logging.getLogger(__name__)
        self.s3_client = boto3.client('s3', region_name=Config.REGION)

        # 상태 캐시 (메모리에서 중복 S3 호출 방지)
        self._state_cache = {}
        self._cache_timestamp = None
        self._cache_ttl_seconds = 60  # 1분 캐시

    def _is_cache_valid(self) -> bool:
        """캐시가 유효한지 확인합니다."""
        if not self._cache_timestamp:
            return False

        cache_age = (datetime.now(timezone.utc) - self._cache_timestamp).total_seconds()
        return cache_age < self._cache_ttl_seconds

    def _load_from_s3(self) -> Dict[str, Any]:
        """S3에서 전체 상태를 로드합니다."""
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=self.state_key
            )

            content = response['Body'].read().decode('utf-8')
            state_data = json.loads(content)

            # 캐시 업데이트
            self._state_cache = state_data
            self._cache_timestamp = datetime.now(timezone.utc)

            self.logger.info(f"S3 상태 로드 완료(s3://{self.bucket_name}/{self.state_key}): {len(state_data)}개 로그그룹")
            return state_data

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'NoSuchKey':
                self.logger.info("S3 상태 파일이 존재하지 않음. 새로 생성합니다.")
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

    def _save_to_s3(self, state_data: Dict[str, Any]) -> bool:
        """S3에 전체 상태를 저장합니다."""
        try:
            # 메타데이터 추가
            metadata = {
                'last_updated': datetime.now(timezone.utc).isoformat(),
                'log_groups_count': str(len(state_data)),
                'manager_version': '2.0'
            }

            json_content = json.dumps(
                state_data,
                indent=2,
                ensure_ascii=False,
                default=str
            )

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=self.state_key,
                Body=json_content.encode('utf-8'),
                ContentType='application/json',
                ServerSideEncryption='AES256',
                Metadata=metadata
            )

            # 캐시 업데이트
            self._state_cache = state_data
            self._cache_timestamp = datetime.now(timezone.utc)

            self.logger.info(f"S3 상태 저장 완료: {len(state_data)}개 로그그룹")
            return True

        except ClientError as e:
            self.logger.error(f"S3 상태 저장 실패: {e}")
            return False
        except Exception as e:
            self.logger.error(f"예상치 못한 S3 저장 오류: {e}")
            return False

    def load_state(self, log_group_name: str) -> Dict[str, Any]:
        """특정 로그그룹의 상태를 로드합니다."""
        try:
            # 캐시에서 먼저 확인
            if self._is_cache_valid() and self._state_cache:
                state_data = self._state_cache
                self.logger.debug(f"캐시에서 상태 로드: {log_group_name}")
            else:
                # S3에서 로드
                state_data = self._load_from_s3()

            # 해당 로그그룹의 상태 반환
            return state_data.get(log_group_name, self._get_default_state(log_group_name))

        except Exception as e:
            self.logger.error(f"상태 로드 실패: {e}")
            return self._get_default_state(log_group_name)

    def save_state(self, log_group_name: str, state: Dict[str, Any]) -> bool:
        """특정 로그그룹의 상태를 저장합니다."""
        try:
            # 현재 전체 상태 로드
            if self._is_cache_valid() and self._state_cache:
                all_states = self._state_cache.copy()
            else:
                all_states = self._load_from_s3()

            # 해당 로그그룹의 상태 업데이트
            all_states[log_group_name] = state

            # S3에 저장
            success = self._save_to_s3(all_states)

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
        try:
            state = self.load_state(log_group_name)
            state['last_run_time'] = datetime.now(timezone.utc).isoformat()
            return self.save_state(log_group_name, state)
        except Exception as e:
            self.logger.error(f"마지막 실행 시간 업데이트 실패: {e}")
            return False

    def update_last_read_time(self, log_group_name: str, read_time: datetime) -> bool:
        """로그그룹의 마지막 읽은 시간을 업데이트합니다."""
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

    def get_state_info(self) -> Dict[str, Any]:
        """상태 파일 정보를 반환합니다."""
        try:
            # S3 객체 메타데이터 확인
            response = self.s3_client.head_object(
                Bucket=self.bucket_name,
                Key=self.state_key
            )

            return {
                'bucket': self.bucket_name,
                'key': self.state_key,
                'size': response.get('ContentLength', 0),
                'last_modified': response.get('LastModified'),
                'metadata': response.get('Metadata', {}),
                'cache_valid': self._is_cache_valid(),
                'cache_timestamp': self._cache_timestamp
            }

        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                return {
                    'bucket': self.bucket_name,
                    'key': self.state_key,
                    'exists': False,
                    'cache_valid': self._is_cache_valid()
                }
            else:
                raise

    def validate_s3_access(self) -> bool:
        """S3 접근 권한을 확인합니다."""
        try:
            # 버킷 존재 확인
            self.s3_client.head_bucket(Bucket=self.bucket_name)

            # 상태 파일 읽기 시도
            try:
                self._load_from_s3()
                self.logger.info(f"S3 접근 권한 확인 완료: {self.bucket_name}")
                return True
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchKey':
                    # 파일이 없는 것은 정상 (처음 실행)
                    self.logger.info(f"S3 접근 권한 확인 완료 (파일 없음): {self.bucket_name}")
                    return True
                else:
                    raise

        except ClientError as e:
            self.logger.error(f"S3 접근 권한 확인 실패: {e}")
            return False
        except NoCredentialsError:
            self.logger.error("AWS 자격 증명이 없습니다.")
            return False
        except Exception as e:
            self.logger.error(f"S3 접근 권한 확인 중 오류: {e}")
            return False
