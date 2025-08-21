import os
import boto3
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any


class Config:
    """AWS CloudWatch Log to S3 설정 클래스"""

    # AWS 설정
    REGION = os.getenv('REGION', 'ap-northeast-2')

    # CloudWatch Log 설정 (단일 로그그룹 - 하위 호환성)
    LOG_GROUP_NAME = os.getenv('LOG_GROUP_NAME', '')
    LOG_STREAM_PREFIX = os.getenv('LOG_STREAM_PREFIX', '')
    MAX_RESULTS = int(os.getenv('MAX_RESULTS', 50))

    # 시간 설정
    MINUTES_BACK = int(os.getenv('MINUTES_BACK', 30))  # 기본 30분 전부터

    # S3 설정
    S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME', '')
    S3_PREFIX = os.getenv('S3_PREFIX', 'CloudWatchLogs')

    # S3 상태 관리 설정
    S3_STATE_KEY = os.getenv('S3_STATE_KEY', f'{S3_PREFIX}State/state.json')
    USE_S3_STATE = os.getenv('USE_S3_STATE', 'true').lower() == 'true'

    # 압축 설정
    COMPRESSION_FORMAT = 'gzip'

    @classmethod
    def get_cloudwatch_retention_days(cls, log_group_name: str) -> int:
        """CloudWatch Log Group의 Retention 설정을 가져옵니다."""
        logger = logging.getLogger(__name__)
        try:
            client = boto3.client('logs', region_name=cls.REGION)
            response = client.describe_log_groups(
                logGroupNamePrefix=log_group_name,
                limit=1
            )

            log_groups = response.get('logGroups', [])
            if log_groups:
                log_group = log_groups[0]
                retention_days = log_group.get('retentionInDays')
                logger.info(f"로그그룹 {log_group_name} 정보: {log_group} Retention 필드 값: {retention_days}")
                if retention_days:
                    logger.info(f"Retention 설정: {retention_days}일")
                    return retention_days
                else:
                    # Retention이 설정되지 않은 경우 (무제한)
                    logger.info("Retention 설정 없음 (무제한)")
                    return 0
            else:
                logger.warning(f"로그그룹을 찾을 수 없습니다: {log_group_name}")
                return 0

        except Exception as e:
            logger.warning(f"CloudWatch Retention 설정 조회 실패 (권한 부족): {e}")
            logger.info("기본 MINUTES_BACK 사용")
            return 0

    @classmethod
    def get_time_range(
        cls,
        log_group_name: str = None,
        retention_day: int = None,
        last_read_time: datetime = None,
        last_run_time: datetime = None
    ):
        """로그를 가져올 시간 범위를 계산합니다."""
        logger = logging.getLogger(__name__)
        current_time = datetime.now(timezone.utc)

        # retention_day가 설정된 경우
        if retention_day is not None and log_group_name:
            # CloudWatch Log의 Retention 설정 가져오기
            cloudwatch_retention_days = cls.get_cloudwatch_retention_days(log_group_name)

            # Retention 설정에서 retention_day를 뺀 만큼의 시간을 계산 (무제한 포함)
            effective_retention_days = max(0, cloudwatch_retention_days - retention_day)

            # Retention 기반 시간 범위 계산
            # Retention 삭제 시점 = 현재시간 - effective_retention_days
            retention_deletion_time = current_time - timedelta(days=effective_retention_days)

            # 상태 관리에서 마지막 읽은 시간이 있으면 연속적으로 계산
            if last_read_time:
                # 복구 로직: 마지막 실행 시간과 현재 시간의 차이가 MINUTES_BACK 보다 크거나 같으면 상태 무시
                if last_run_time:
                    time_diff = current_time - last_run_time
                else:
                    time_diff = current_time - last_read_time

                recovery_threshold = timedelta(minutes=cls.MINUTES_BACK * 1)  # 30분

                if time_diff >= recovery_threshold:
                    # 복구 모드: 처음 실행과 동일하게 Retention 기반 계산 사용
                    start_time = retention_deletion_time - timedelta(minutes=cls.MINUTES_BACK)
                    end_time = retention_deletion_time
                    logger.warning(
                        f"{log_group_name}: 복구 모드 - 마지막 실행 후 {time_diff.total_seconds() / 60:.1f}분 경과 "
                        f"(임계값: {cls.MINUTES_BACK}분) - {start_time} ~ {end_time}"
                    )
                else:
                    # 정상 모드: 상태 기반 연속 읽기
                    # last_read_time부터 현재 실행 시간의 retention_deletion_time까지 읽기
                    start_time = last_read_time
                    end_time = retention_deletion_time
                    logger.info(f"{log_group_name}: 상태 기반 연속 읽기 - {start_time} ~ {end_time}")
            else:
                # 처음 실행이거나 상태가 없는 경우
                # Retention 삭제 시점 기준으로 MINUTES_BACK만큼 읽기
                start_time = retention_deletion_time - timedelta(minutes=cls.MINUTES_BACK)
                end_time = retention_deletion_time
                logger.info(f"{log_group_name}: 초기 실행 - {start_time} ~ {end_time}")

            logger.info(
                f"{log_group_name}: Retention {cloudwatch_retention_days}일 - "
                f"{retention_day}일 = {effective_retention_days}일 범위"
            )
            logger.info(f"{log_group_name}: MINUTES_BACK {cls.MINUTES_BACK}분 적용")
        else:
            # 기존 방식 (retention_day가 설정되지 않은 경우)
            if last_read_time:
                # 복구 로직: 마지막 읽은 시간과 현재 시간의 차이가 MINUTES_BACK * 2보다 크면 상태 무시
                time_diff = current_time - last_read_time
                recovery_threshold = timedelta(minutes=cls.MINUTES_BACK * 2)

                if time_diff >= recovery_threshold:
                    # 복구 모드: 상태를 무시하고 현재 시간 기준으로 읽기
                    start_time = current_time - timedelta(minutes=cls.MINUTES_BACK)
                    end_time = current_time
                    logger.warning(
                        f"{log_group_name}: 복구 모드 (기본) - 마지막 실행 후 {time_diff.total_seconds() / 60:.1f}분 경과 "
                        f"(임계값: {cls.MINUTES_BACK * 2}분) - {start_time} ~ {end_time}"
                    )
                else:
                    # 정상 모드: 상태 기반 연속 읽기
                    start_time = last_read_time
                    end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                    logger.info(f"{log_group_name}: 상태 기반 연속 읽기 (기본) - {start_time} ~ {end_time}")
            else:
                start_time = current_time - timedelta(minutes=cls.MINUTES_BACK)
                end_time = current_time
                logger.info(f"{log_group_name}: 초기 실행 (기본) - {start_time} ~ {end_time}")

        return start_time, end_time

    @classmethod
    def get_s3_key(cls, log_group_name: str, start_time: datetime, end_time: datetime, stream_name: str = None) -> str:
        """S3 저장 키를 생성합니다."""
        # 로그그룹 이름에서 '/' 제거 및 정리
        clean_group_name = log_group_name.replace('/', '_').replace(' ', '_')

        # 시작 시간과 종료 시간을 파일명에 포함 (밀리초 포함)
        start_time_str = start_time.strftime('%Y%m%d_%H%M%S_%f')[:-3]  # 밀리초 3자리만
        end_time_str = end_time.strftime('%Y%m%d_%H%M%S_%f')[:-3]    # 밀리초 3자리만
        time_range_str = f"{start_time_str}_{end_time_str}"

        # 날짜별 디렉토리 생성 (시작 시간 기준)
        date_path = start_time.strftime('%Y/%m/%d')

        # 로그 스트림 이름 정리 (있는 경우)
        if stream_name:
            clean_stream_name = stream_name.replace('/', '_').replace(' ', '_')
            # CloudWatchLogs/로그그룹/로그스트림/연도/월/일/시분초_start_end.log.gz 형태
            return (
                f"CloudWatchLogs/{clean_group_name}/{clean_stream_name}/{date_path}/"
                f"{time_range_str}.log.gz"
            )
        else:
            # 기존 방식 (로그스트림이 없는 경우)
            return f"CloudWatchLogs/{clean_group_name}/{date_path}/{time_range_str}.log.gz"

    @classmethod
    def get_batch_configs(cls) -> List[Dict[str, Any]]:
        """배치 처리를 위한 설정 목록을 반환합니다."""
        logger = logging.getLogger(__name__)
        configs = []

        # 환경변수에서 여러 로그그룹 설정 파싱
        i = 1
        while True:
            log_group_name = os.getenv(f'LOG_GROUP_NAME_{i}', '')
            if not log_group_name:
                break

            stream_prefixes_str = os.getenv(f'LOG_STREAM_PREFIX_{i}', '')
            stream_prefixes = []

            if stream_prefixes_str:
                # 쉼표로 구분된 접두사들을 파싱
                stream_prefixes = [prefix.strip() for prefix in stream_prefixes_str.split(',')]
                # 길이가 긴 순서대로 정렬 (더 정확한 매칭을 위해)
                stream_prefixes.sort(key=len, reverse=True)

            # retention_day 설정 가져오기
            retention_day_str = os.getenv(f'LOG_GROUP_RETENTION_DAY_{i}', '')
            retention_day = None
            if retention_day_str and retention_day_str.strip():
                try:
                    retention_day = int(retention_day_str.strip())
                except ValueError:
                    logger = logging.getLogger(__name__)
                    logger.warning(f"잘못된 retention_day 값: '{retention_day_str}' (로그그룹 {i})")

            configs.append({
                'log_group_name': log_group_name,
                'stream_prefixes': stream_prefixes,
                'retention_day': retention_day,
                'index': i
            })
            i += 1

        return configs

    @classmethod
    def get_max_workers(cls) -> int:
        """최대 워커 스레드 수를 반환합니다."""
        return int(os.getenv('MAX_WORKERS', 4))

    @classmethod
    def validate_s3_config(cls):
        """S3 설정을 검증합니다."""
        logger = logging.getLogger(__name__)
        if cls.S3_BUCKET_NAME == '':
            logger.error("S3_BUCKET_NAME이 기본값으로 설정되어 있습니다!")
            return False

        return True
