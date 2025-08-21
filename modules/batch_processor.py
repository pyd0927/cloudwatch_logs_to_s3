import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Any
from .config import Config
from .cloudwatch_log_reader import CloudWatchLogReader
from .log_compressor import LogCompressor
from .s3_uploader import S3Uploader
from .s3_state_manager import S3StateManager
import json


class BatchProcessor:
    """멀티스레딩을 활용한 배치 로그 처리 클래스"""

    def __init__(self, max_workers: int = None):
        self.max_workers = max_workers or Config.get_max_workers()
        self.logger = logging.getLogger(__name__)

        self.state_manager = S3StateManager()

        self.s3_uploader = S3Uploader()
        self.compressor = LogCompressor()

    def process_single_log_group(
        self, config: Dict[str, Any],
        start_time: datetime, end_time: datetime,
    ) -> Dict[str, Any]:
        """단일 로그그룹을 처리합니다."""
        log_group_name = config['log_group_name']
        stream_prefixes = config['stream_prefixes']

        result = {
            'log_group_name': log_group_name,
            'stream_prefixes': stream_prefixes,
            'success': False,
            'logs_count': 0,
            'uploaded_files': [],
            'error': None
        }

        try:
            self.logger.info(f"로그그룹 처리: {log_group_name}")

            all_logs = []
            all_active_streams = []

            # 접두사를 길이 순으로 정렬 (긴 접두사부터 처리하여 정확한 매칭)
            sorted_prefixes = sorted(stream_prefixes, key=len, reverse=True)
            self.logger.info(f"접두사 처리 순서 (길이 순): {sorted_prefixes}")

            for stream_prefix in sorted_prefixes:
                # CloudWatch에서 로그 가져오기
                reader = CloudWatchLogReader(state_manager=self.state_manager)

                logs = reader.get_all_logs_with_state(
                    log_group_name, stream_prefix, start_time, end_time
                )

                # 활성 스트림 수집 (로그가 없어도 스트림 자체는 활성으로 간주)
                for stream in reader.get_log_streams(log_group_name, stream_prefix, start_time, end_time):
                    if stream['logStreamName'] not in all_active_streams:
                        all_active_streams.append(stream['logStreamName'])

                if logs:
                    # 스트림별로 로그를 그룹핑
                    logs_by_stream = {}
                    for log in logs:
                        # 로그에서 스트림 이름 추출 (logStream 필드 또는 추론)
                        stream_name = log.get('logStream', 'unknown')
                        if stream_name not in logs_by_stream:
                            logs_by_stream[stream_name] = []
                        logs_by_stream[stream_name].append(log)

                    self.logger.info(f"{stream_prefix}: {len(logs_by_stream)}개 스트림에서 총 {len(logs)}개 로그 수집")

                    # LOG_STREAM_PREFIX별로 하나의 파일 생성 (모든 스트림의 로그를 합침)
                    s3_key = Config.get_s3_key(log_group_name, start_time, end_time, stream_prefix)

                    # 로그 압축 (모든 스트림의 로그를 하나로 합쳐서 압축)
                    compressed_data = self.compressor.compress_logs(
                        logs, format_type='json', filename=s3_key
                    )

                    # 압축 결과 검증
                    if not compressed_data:
                        self.logger.error(f"압축 실패: {stream_prefix}")
                        continue

                    if not compressed_data.startswith(b'\x1f\x8b'):
                        self.logger.error(f"압축 데이터에 gzip 헤더 없음: {stream_prefix}")
                        self.logger.error(f"예상 헤더: 1f8b, 실제 헤더: {compressed_data[:2].hex()}")
                        continue

                    # 압축 비율 계산
                    original_size = sum(len(json.dumps(log, ensure_ascii=False)) for log in logs)
                    compression_ratio = self.compressor.get_compression_ratio(original_size, len(compressed_data))

                    self.logger.info(
                        f"압축 완료: {stream_prefix} ({len(compressed_data)} bytes, {compression_ratio:.1f}% 압축)")

                    # S3에 업로드
                    upload_success = self.s3_uploader.upload_compressed_logs(
                        Config.S3_BUCKET_NAME,
                        s3_key,
                        compressed_data
                    )

                    if upload_success:
                        result['uploaded_files'].append({
                            'stream_prefix': stream_prefix,
                            's3_key': s3_key,
                            'logs_count': len(logs),
                            'original_size': original_size,
                            'compressed_size': len(compressed_data),
                            'compression_ratio': round(compression_ratio, 1)
                        })
                        self.logger.info(
                            f"✅ {stream_prefix}: {len(logs)}개 → S3 업로드 완료 ({compression_ratio:.1f}% 압축)")
                    else:
                        self.logger.error(f"❌ {stream_prefix}: S3 업로드 실패")
                else:
                    self.logger.warning(f"⚠️ {stream_prefix}: 수집된 로그 없음")

                all_logs.extend(logs)

            result['logs_count'] = len(all_logs)
            result['success'] = len(result['uploaded_files']) > 0

            if result['success']:
                log_count = result['logs_count']
                file_count = len(result['uploaded_files'])
                self.logger.info(f"✅ 완료: {log_group_name}")
                self.logger.info(f"   총 {log_count}개 로그 → {file_count}개 파일 업로드")

                # 업로드된 파일 목록 표시
                for file_info in result['uploaded_files']:
                    self.logger.info(f"   {file_info['s3_key']} ({file_info['logs_count']}개 로그)")

            # 모든 스트림을 활성 스트림으로 정리
            self.state_manager.cleanup_old_streams(log_group_name, all_active_streams)
            self.state_manager.update_last_read_time(log_group_name, end_time)
            self.state_manager.update_last_run_time(log_group_name)

        except Exception as e:
            result['error'] = str(e)
            self.logger.error(f"❌ 처리 오류: {log_group_name} - {e}")
            import traceback
            self.logger.error(f"스택 트레이스: {traceback.format_exc()}")

        return result

    def process_all_log_groups(
        self, start_time: datetime = None,
        end_time: datetime = None,
    ) -> List[Dict[str, Any]]:
        """모든 로그그룹을 멀티스레딩으로 처리합니다."""
        configs = Config.get_batch_configs()

        if not configs:
            self.logger.warning("처리할 로그그룹이 설정되지 않았습니다.")
            return []

        self.logger.info(f"배치 처리 시작: {len(configs)}개 로그그룹, {self.max_workers}개 워커")

        results = []

        # ThreadPoolExecutor를 사용한 멀티스레딩 처리 (타임아웃 4분)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 각 로그그룹을 별도 스레드에서 처리
            future_to_config = {}
            for config in configs:
                # 각 로그그룹별로 개별 시간 범위 계산
                log_group_name = config['log_group_name']
                retention_day = config.get('retention_day')

                if start_time is None or end_time is None:
                    # 상태 관리에서 마지막 읽은 시간과 실행 시간 가져오기
                    last_read_time = None
                    last_run_time = None
                    if Config.USE_S3_STATE:
                        last_read_time = self.state_manager.get_last_read_time(log_group_name)
                        last_run_time = self.state_manager.get_last_run_time(log_group_name)

                    # retention_day 설정이 있으면 해당 설정으로 시간 범위 계산
                    group_start_time, group_end_time = Config.get_time_range(
                        log_group_name, retention_day, last_read_time, last_run_time
                    )
                else:
                    # 전역 시간 범위 사용
                    group_start_time, group_end_time = start_time, end_time

                future = executor.submit(
                    self.process_single_log_group,
                    config,
                    group_start_time,
                    group_end_time
                )
                future_to_config[future] = config

            # 완료된 작업들의 결과 수집 (타임아웃 4분)
            for future in as_completed(future_to_config, timeout=240):
                config = future_to_config[future]
                try:
                    result = future.result(timeout=240)  # 각 작업당 4분 타임아웃
                    results.append(result)

                    if result['success']:
                        self.logger.info(f"성공: {result['log_group_name']} - {result['logs_count']}개 로그")
                    else:
                        self.logger.error(f"실패: {result['log_group_name']} - {result['error']}")

                except Exception as e:
                    self.logger.error(f"스레드 실행 중 오류: {config['log_group_name']} - {e}")
                    results.append({
                        'log_group_name': config['log_group_name'],
                        'config_index': config['index'],
                        'success': False,
                        'error': str(e)
                    })

        # 결과 요약
        successful_count = sum(1 for r in results if r['success'])
        total_logs = sum(r.get('logs_count', 0) for r in results)

        self.logger.info(f"배치 처리 완료: {successful_count}/{len(configs)} 성공, 총 {total_logs}개 로그")

        return results
