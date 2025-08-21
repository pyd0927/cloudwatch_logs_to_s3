import boto3
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Generator
from .config import Config
from .s3_state_manager import S3StateManager


class CloudWatchLogReader:
    """CloudWatch Log에서 로그스트림을 읽는 클래스"""

    def __init__(self, region_name: str = None, state_manager=None):
        self.region_name = region_name or Config.REGION
        self.client = boto3.client('logs', region_name=self.region_name)
        self.logger = logging.getLogger(__name__)
        self.matched_streams = set()  # 이미 매칭된 스트림 추적

        if state_manager:
            self.state_manager = state_manager
        else:
            self.state_manager = S3StateManager()
            self.logger.info("S3 기반 상태 관리 사용")

    def _filter_streams_by_exact_prefix(self, streams: List[Dict[str, Any]], prefix: str) -> List[Dict[str, Any]]:
        """정확한 접두사 매칭으로 로그 스트림을 필터링합니다."""
        if not prefix:
            return streams

        filtered_streams = []
        matched_count = 0
        excluded_count = 0
        already_matched_count = 0

        for stream in streams:
            stream_name = stream['logStreamName']

            # 이미 다른 접두사와 매칭된 스트림은 제외
            if stream_name in self.matched_streams:
                already_matched_count += 1
                continue

            # Container Insights 패턴: kube-proxy-h6msz_kube-system_kube-proxy-... 형태
            # 접두사가 스트림 이름의 특정 위치에 정확히 매칭되는지 확인
            if self._matches_container_insights_pattern(stream_name, prefix):
                filtered_streams.append(stream)
                matched_count += 1
                # 매칭된 스트림을 추적에 추가
                self.matched_streams.add(stream_name)
            else:
                excluded_count += 1

        self.logger.info(
            f"🔍 {prefix}: {matched_count}개 매칭, {excluded_count}개 제외, {already_matched_count}개 이미 매칭됨")
        return filtered_streams

    def _matches_container_insights_pattern(self, stream_name: str, prefix: str) -> bool:
        """로그스트림 이름 패턴에 맞는지 확인합니다 (기존 + Container Insights 패턴)."""

        # 1. 기존 정확한 접두사 패턴: kube-proxy-abc123 형태
        # prefix 뒤에 하이픈이 오고 그 다음에 문자나 숫자가 오고, .log로 끝나는 경우
        escaped_prefix = re.escape(prefix).replace('\\-', '-').replace('\\_', '_')
        exact_pattern = re.compile(r'^({})-[a-zA-Z0-9]+(\.log)?$'.format(escaped_prefix))

        if exact_pattern.match(stream_name):
            return True

        # 2. Container Insights 패턴:
        # ip-10-0-3-92.ap-northeast-2.compute.internal-dataplane.tail.var.log.containers.kube-proxy-h6msz_kube-system_kube-proxy-...
        # 패턴: .kube-proxy-h6msz_kube-system_kube-proxy-... 형태에서 두 번째 kube-proxy가 실제 서비스 이름
        container_insights_pattern = rf'\.{re.escape(prefix)}-[a-zA-Z0-9]+_{re.escape(prefix)}-'

        if re.search(container_insights_pattern, stream_name):
            return True

        # 3. 정확한 접두사 매칭: 스트림 이름이 정확히 접두사로 시작하는지 확인
        # 예: kube-apiserver로 시작하는 스트림만 매칭 (kube-apiserver-audit은 제외)
        if stream_name.lower().startswith(prefix.lower()):
            # 접두사 다음에 하이픈이나 언더스코어가 오는 경우만 매칭
            # 예: kube-apiserver-xxx (매칭), kube-apiserver-audit-xxx (매칭 안됨)
            if len(stream_name) > len(prefix):
                next_char = stream_name[len(prefix)]
                if next_char in ['-', '_', '.']:
                    return True
            else:
                # 접두사와 정확히 일치하는 경우
                return True

        return False

    def _is_container_insights_pattern(self, prefix: str) -> bool:
        """Container Insights 패턴인지 확인합니다."""
        # Container Insights에서 사용하는 일반적인 접두사들
        container_insights_prefixes = [
            'kube-proxy', 'kube-system', 'kube-apiserver', 'kube-scheduler',
            'kube-controller-manager', 'authenticator', 'fluentd', 'aws-node'
        ]
        return prefix in container_insights_prefixes

    def get_log_streams(
        self, log_group_name: str, stream_prefix: str = None,
        start_time: datetime = None, end_time: datetime = None
    ) -> List[Dict[str, Any]]:
        """로그그룹에서 로그스트림 목록을 가져옵니다."""
        all_streams = []

        try:
            # 시간 범위를 밀리초로 변환
            from_time = int(start_time.timestamp() * 1000) if start_time else None
            to_time = int(end_time.timestamp() * 1000) if end_time else None

            params = {
                'logGroupName': log_group_name,
                'limit': Config.MAX_RESULTS
            }

            # Container Insights 패턴의 경우 logStreamNamePrefix를 사용하지 않음
            # (스트림 이름이 ip-10-0-3-92... 형태로 시작하기 때문)
            if stream_prefix and not self._is_container_insights_pattern(stream_prefix):
                params['logStreamNamePrefix'] = stream_prefix
            else:
                # Container Insights 또는 접두사가 없을 때는 정렬 사용
                params['orderBy'] = 'LastEventTime'
                params['descending'] = True

            while True:
                response = self.client.describe_log_streams(**params)
                streams = response.get('logStreams', [])

                # 정확한 접두사 매칭 필터링 적용
                if stream_prefix:
                    streams = self._filter_streams_by_exact_prefix(streams, stream_prefix)

                # 시간 범위와 겹치는 스트림만 필터링
                for stream in streams:
                    stream_name = stream['logStreamName']
                    last_event_time = stream.get('lastEventTime', 0)
                    first_event_time = stream.get('firstEventTime', 0)

                    # 시간 정보가 있는 경우 시간 범위 확인
                    if from_time and to_time and first_event_time > 0 and last_event_time > 0:
                        # 스트림의 시간 범위와 요청한 시간 범위가 겹치는지 확인
                        if not (last_event_time < from_time or first_event_time > to_time):
                            all_streams.append(stream)
                            self.logger.debug(f"스트림 포함 (시간 범위 일치): {stream_name}")
                    else:
                        # 시간 정보가 없거나 시간 범위가 설정되지 않은 경우 모두 포함
                        all_streams.append(stream)
                        self.logger.debug(f"스트림 포함 (시간 범위 없음): {stream_name}")

                # 다음 페이지가 있는지 확인
                if not response.get('nextToken'):
                    break

                params['nextToken'] = response['nextToken']
                self.logger.info(f"로그스트림 페이지 처리 중: {len(streams)}개 추가")

            self.logger.info(f"총 {len(all_streams)}개의 로그스트림을 가져왔습니다.")
            return all_streams

        except Exception as e:
            self.logger.error(f"로그스트림 목록 가져오기 실패: {e}")
            return all_streams

    def get_log_events_with_state(
        self, log_group_name: str, log_stream_name: str,
        start_time: datetime = None, end_time: datetime = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """상태를 고려하여 특정 로그스트림에서 로그 이벤트를 가져옵니다."""
        try:
            stream_state = self.state_manager.get_stream_state(log_group_name, log_stream_name)
            params = {
                'logGroupName': log_group_name,
                'logStreamName': log_stream_name
            }

            # 상태가 있으면 이전 위치부터 읽기 (end_time까지만)
            if stream_state and stream_state.get('next_token'):
                # 날짜가 바뀌었는지 확인 (토큰 무효화 조건)
                last_event_time = stream_state.get('last_event_time')
                use_token = True

                if last_event_time and start_time:
                    # Unix timestamp를 datetime으로 변환
                    last_event_dt = datetime.fromtimestamp(last_event_time / 1000, tz=timezone.utc)

                    # 날짜가 바뀌었으면 토큰 무시
                    if last_event_dt.date() != start_time.date():
                        use_token = False
                        self.logger.info(
                            f"날짜 변경 감지: {log_stream_name} "
                            f"(이전: {last_event_dt.date()}, 현재: {start_time.date()}) - 토큰 무시"
                        )

                if use_token:
                    params['nextToken'] = stream_state['next_token']
                    # end_time이 설정되어 있으면 상태의 시간부터 end_time까지만 읽기
                    if end_time:
                        params['endTime'] = int(end_time.timestamp() * 1000)
                        self.logger.info(
                            f"상태 기반 읽기: {log_stream_name} (토큰: {stream_state['next_token'][:20]}..., 종료: {end_time})")
                    else:
                        self.logger.info(f"상태 기반 읽기: {log_stream_name} (토큰: {stream_state['next_token'][:20]}...)")
                else:
                    # 토큰 무시하고 새로 읽기
                    if start_time:
                        params['startTime'] = int(start_time.timestamp() * 1000)
                        self.logger.info(f"시작 시간 설정: {start_time} ({params['startTime']})")
                    if end_time:
                        params['endTime'] = int(end_time.timestamp() * 1000)
                        self.logger.info(f"종료 시간 설정: {end_time} ({params['endTime']})")
                    self.logger.info(f"토큰 무시하고 새로 읽기 시작: {log_stream_name}")
            else:
                # 처음 읽는 경우에만 전체 시간 범위 설정
                if start_time:
                    params['startTime'] = int(start_time.timestamp() * 1000)
                    self.logger.info(f"시작 시간 설정: {start_time} ({params['startTime']})")
                if end_time:
                    params['endTime'] = int(end_time.timestamp() * 1000)
                    self.logger.info(f"종료 시간 설정: {end_time} ({params['endTime']})")
                self.logger.info(f"새로 읽기 시작: {log_stream_name}")

            last_event_time = None
            next_token = None
            total_events = 0
            consecutive_empty_responses = 0
            max_consecutive_empty = 3
            max_iterations = 10  # 과거 시간 범위에서 무한 루프 방지
            iteration_count = 0

            while True:
                iteration_count += 1
                if iteration_count > max_iterations:
                    self.logger.warning(f"최대 반복 횟수 초과로 종료: {log_stream_name} (과거 시간 범위)")
                    break
                try:
                    response = self.client.get_log_events(**params)
                    events = response.get('events', [])

                    for event in events:
                        last_event_time = event.get('timestamp', 0)
                        total_events += 1
                        # 이벤트에 스트림 이름 추가
                        event['logStream'] = log_stream_name
                        yield event

                    next_forward_token = response.get('nextForwardToken')

                    # 종료 조건 확인
                    if not next_forward_token:
                        break

                    if len(events) == 0:
                        consecutive_empty_responses += 1
                    else:
                        consecutive_empty_responses = 0

                    if consecutive_empty_responses >= max_consecutive_empty:
                        self.logger.info(f"연속 빈 응답으로 종료: {log_stream_name}")
                        break

                    if next_forward_token == params.get('nextToken'):
                        self.logger.info(f"토큰 반복으로 종료: {log_stream_name}")
                        break

                    next_token = next_forward_token
                    params['nextToken'] = next_token
                except Exception as api_error:
                    self.logger.error(f"API 호출 오류: {log_stream_name} - {api_error}")
                    break

            if total_events > 0:
                self.logger.info(f"✅ {log_stream_name}: {total_events}개 이벤트 수집")

            # 상태 업데이트 - 연속 실행을 위해 실제 마지막 이벤트 시간 사용
            if next_token or last_event_time:
                # 실제 마지막 이벤트 시간 사용 (end_time이 아닌)
                state_time_ms = last_event_time if last_event_time else int(end_time.timestamp() * 1000)

                self.state_manager.update_stream_state(
                    log_group_name, log_stream_name, next_token, state_time_ms
                )
                self.logger.info(
                    f"상태 업데이트 완료: {log_stream_name} (토큰: {next_token[:20] if next_token else 'None'}, "
                    f"시간: {state_time_ms})")
            else:
                self.logger.info(f"상태 업데이트 없음: {log_stream_name} (이벤트 없음)")

        except Exception as e:
            self.logger.error(f"로그 이벤트 가져오기 실패: {log_stream_name} - {e}")
            import traceback
            self.logger.error(f"스택 트레이스: {traceback.format_exc()}")

    def get_all_logs_with_state(
        self, log_group_name: str, stream_prefix: str = None,
        start_time: datetime = None, end_time: datetime = None,
    ) -> List[Dict[str, Any]]:
        """상태를 고려하여 모든 로그스트림에서 로그를 가져옵니다."""
        all_logs = []
        active_streams = []

        # 시간 범위를 get_log_streams에 전달하여 초기 필터링
        log_streams = self.get_log_streams(log_group_name, stream_prefix, start_time, end_time)
        self.logger.info(f"📋 {len(log_streams)}개 스트림 발견: {stream_prefix or 'all'}")

        for i, stream in enumerate(log_streams, 1):
            stream_name = stream['logStreamName']
            active_streams.append(stream_name)

            stored_bytes = stream.get('storedBytes', 0)
            if stored_bytes == 0:
                self.logger.debug(f"빈 스트림 스킵: {stream_name}")

            stream_event_count = 0
            for event in self.get_log_events_with_state(log_group_name, stream_name, start_time, end_time):
                all_logs.append(event)
                stream_event_count += 1

        if all_logs:
            self.logger.info(f"{stream_prefix or 'all'}: 총 {len(all_logs)}개 로그 이벤트 수집 완료")
        else:
            self.logger.warning(f"{stream_prefix or 'all'}: 수집된 로그 없음")

        return all_logs

    def get_log_events(
        self, log_group_name: str, log_stream_name: str,
        start_time: datetime = None, end_time: datetime = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """기존 메서드 (하위 호환성 유지)"""
        return self.get_log_events_with_state(log_group_name, log_stream_name, start_time, end_time)

    def get_all_logs(
        self, log_group_name: str, stream_prefix: str = None,
        start_time: datetime = None, end_time: datetime = None,
    ) -> List[Dict[str, Any]]:
        """기존 메서드 (하위 호환성 유지)"""
        return self.get_all_logs_with_state(log_group_name, stream_prefix, start_time, end_time)
