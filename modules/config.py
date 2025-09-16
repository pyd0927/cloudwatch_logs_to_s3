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

        # retention_day가 명시적으로 설정된 경우 (0보다 큰 값)
        if retention_day is not None and retention_day > 0 and log_group_name:
            # CloudWatch Log의 Retention 설정 가져오기
            cloudwatch_retention_days = cls.get_cloudwatch_retention_days(log_group_name)

            # 무제한 Retention (0)인 경우 전체 백업 모드로 처리
            if cloudwatch_retention_days == 0:
                logger.info(f"{log_group_name}: 무제한 Retention 감지 - 전체 백업 모드로 전환")
                # 전체 백업 모드와 동일한 로직 사용
                if last_read_time:
                    start_time = last_read_time
                    end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                    logger.info(f"{log_group_name}: 무제한 Retention - 상태 기반 연속 읽기 - {start_time} ~ {end_time}")
                else:
                    # CloudWatch Logs에서 실제 가장 오래된 로그 시간을 가져오기
                    try:
                        import boto3
                        client = boto3.client('logs', region_name=cls.REGION)

                        # Container Insights의 경우 describe_log_streams에서 firstEventTime이 제대로 표시되지 않음
                        # startFromHead=True를 사용하여 실제 가장 오래된 로그를 찾기
                        logger.info(f"{log_group_name}: Container Insights 로그그룹 감지 - startFromHead 방식으로 가장 오래된 로그 찾기")

                        # 로그 스트림 목록 가져오기
                        all_streams = []
                        next_token = None

                        while True:
                            params = {
                                'logGroupName': log_group_name,
                                'orderBy': 'LogStreamName',
                                'limit': 50
                            }
                            if next_token:
                                params['nextToken'] = next_token

                            response = client.describe_log_streams(**params)
                            streams = response.get('logStreams', [])
                            all_streams.extend(streams)

                            if not response.get('nextToken'):
                                break
                            next_token = response['nextToken']

                        # 각 스트림에서 startFromHead=True로 가장 오래된 로그 찾기
                        oldest_event_time = None
                        streams_with_events = 0
                        total_streams = len(all_streams)

                        for i, stream in enumerate(all_streams[:10]):  # 처음 10개 스트림만 확인 (성능 고려)
                            stream_name = stream['logStreamName']
                            try:
                                response = client.get_log_events(
                                    logGroupName=log_group_name,
                                    logStreamName=stream_name,
                                    startFromHead=True,
                                    limit=1  # 첫 번째 이벤트만 가져오기
                                )

                                events = response.get('events', [])
                                if events:
                                    streams_with_events += 1
                                    first_event_time = events[0].get('timestamp', 0)
                                    if first_event_time > 0:
                                        if oldest_event_time is None or first_event_time < oldest_event_time:
                                            oldest_event_time = first_event_time

                            except Exception as e:
                                logger.debug(f"스트림 {stream_name}에서 로그 확인 실패: {e}")
                                continue

                        logger.info(f"{log_group_name}: 총 {total_streams}개 스트림 중 {streams_with_events}개에서 로그 발견")

                        if oldest_event_time:
                            # 실제 가장 오래된 로그 시간을 시작점으로 설정
                            start_time = datetime.fromtimestamp(oldest_event_time / 1000, tz=timezone.utc)
                            end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                            logger.info(
                                f"{log_group_name}: 무제한 Retention - 실제 가장 오래된 로그부터 "
                                f"- {start_time} ~ {end_time}"
                            )
                        else:
                            # 로그를 찾을 수 없는 경우 안전한 폴백
                            start_time = current_time - timedelta(days=7)  # 7일 전부터 시작
                            end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                            logger.info(
                                f"{log_group_name}: 무제한 Retention - 로그를 찾을 수 없음 (7일 전부터) "
                                f"- {start_time} ~ {end_time}"
                            )
                    except Exception as e:
                        # 오류 발생 시 1년 전부터 시작
                        start_time = current_time - timedelta(days=365)
                        end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                        logger.warning(
                            f"{log_group_name}: 무제한 Retention - 오류 발생 (1년 전부터) "
                            f"- {e} - {start_time} ~ {end_time}"
                        )

                return start_time, end_time

            # Retention 설정에서 retention_day를 뺀 만큼의 시간을 계산
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
        elif retention_day == -1:
            # 전체 백업 모드 (retention_day=-1)
            logger.info(f"{log_group_name}: 전체 백업 모드로 처리")

            if last_read_time:
                # State 파일에 값이 있으면 기록된 시간부터 MINUTES_BACK만큼 읽기
                start_time = last_read_time
                end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                logger.info(f"{log_group_name}: 전체 백업 모드 - 상태 기반 연속 읽기 - {start_time} ~ {end_time}")
            else:
                # State 파일에 값이 없을 때만 가장 오래된 로그를 찾아서 읽기
                try:
                    import boto3
                    client = boto3.client('logs', region_name=cls.REGION)

                    # Container Insights의 경우 describe_log_streams에서 firstEventTime이 제대로 표시되지 않음
                    # startFromHead=True를 사용하여 실제 가장 오래된 로그를 찾기
                    logger.info(f"{log_group_name}: Container Insights 로그그룹 감지 - startFromHead 방식으로 가장 오래된 로그 찾기")

                    # 로그 스트림 목록 가져오기
                    all_streams = []
                    next_token = None

                    while True:
                        params = {
                            'logGroupName': log_group_name,
                            'orderBy': 'LogStreamName',
                            'limit': 50
                        }
                        if next_token:
                            params['nextToken'] = next_token

                        response = client.describe_log_streams(**params)
                        streams = response.get('logStreams', [])
                        all_streams.extend(streams)

                        if not response.get('nextToken'):
                            break
                        next_token = response['nextToken']

                    # 각 스트림에서 startFromHead=True로 가장 오래된 로그 찾기
                    oldest_event_time = None
                    streams_with_events = 0
                    total_streams = len(all_streams)

                    for i, stream in enumerate(all_streams[:10]):  # 처음 10개 스트림만 확인 (성능 고려)
                        stream_name = stream['logStreamName']
                        try:
                            response = client.get_log_events(
                                logGroupName=log_group_name,
                                logStreamName=stream_name,
                                startFromHead=True,
                                limit=1  # 첫 번째 이벤트만 가져오기
                            )

                            events = response.get('events', [])
                            if events:
                                streams_with_events += 1
                                first_event_time = events[0].get('timestamp', 0)
                                if first_event_time > 0:
                                    if oldest_event_time is None or first_event_time < oldest_event_time:
                                        oldest_event_time = first_event_time

                        except Exception as e:
                            logger.debug(f"스트림 {stream_name}에서 로그 확인 실패: {e}")
                            continue

                    logger.info(f"{log_group_name}: 총 {total_streams}개 스트림 중 {streams_with_events}개에서 로그 발견")

                    if oldest_event_time:
                        # 실제 가장 오래된 로그 시간을 시작점으로 설정
                        start_time = datetime.fromtimestamp(oldest_event_time / 1000, tz=timezone.utc)
                        end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                        logger.info(
                            f"{log_group_name}: 전체 백업 모드 - 실제 가장 오래된 로그부터 "
                            f"- {start_time} ~ {end_time}"
                        )
                    else:
                        # 로그를 찾을 수 없는 경우 안전한 폴백
                        start_time = current_time - timedelta(days=7)  # 7일 전부터 시작
                        end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                        logger.info(
                            f"{log_group_name}: 전체 백업 모드 - 로그를 찾을 수 없음 (7일 전부터) "
                            f"- {start_time} ~ {end_time}"
                        )
                except Exception as e:
                    # 오류 발생 시 1년 전부터 시작
                    start_time = current_time - timedelta(days=365)
                    end_time = start_time + timedelta(minutes=cls.MINUTES_BACK)
                    logger.warning(
                        f"{log_group_name}: 전체 백업 모드 - 오류 발생 (1년 전부터) "
                        f"- {e} - {start_time} ~ {end_time}"
                    )

            return start_time, end_time
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
                # 일반 모드: 현재 시간 기준으로 읽기
                start_time = current_time - timedelta(minutes=cls.MINUTES_BACK)
                end_time = current_time
                logger.info(f"{log_group_name}: 초기 실행 (기본) - {start_time} ~ {end_time}")

        # 시간 범위 검증: 계산된 시간 범위가 State 파일의 last_read_time보다 이전이거나 같은 경우 처리
        if last_read_time and start_time and end_time:
            # 시간대를 고려하여 정확한 비교
            if end_time <= last_read_time:
                logger.warning(
                    f"{log_group_name}: 계산된 시간 범위가 State 파일의 last_read_time보다 이전이거나 같습니다. "
                    f"로그를 읽지 않고 건너뜁니다. "
                    f"계산된 범위: {start_time} ~ {end_time}, "
                    f"State last_read_time: {last_read_time}, "
                    f"시간 차이: {(end_time - last_read_time).total_seconds()}초"
                )
                # 시간 범위를 None으로 설정하여 로그를 읽지 않도록 함
                return None, None

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

            # 전체 로그 백업 모드 설정 가져오기
            full_backup_str = os.getenv(f'LOG_GROUP_FULL_BACKUP_{i}', '').lower()
            full_backup = full_backup_str in ['true', '1', 'yes', 'on']

            configs.append({
                'log_group_name': log_group_name,
                'stream_prefixes': stream_prefixes,
                'retention_day': retention_day,
                'full_backup': full_backup,
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
