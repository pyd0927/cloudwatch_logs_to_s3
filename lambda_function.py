#!/usr/bin/env python3
"""
AWS Lambda 함수 - CloudWatch Log에서 S3로 로그 저장
Retention 삭제 직전 구간을 백업하기 위해 각 로그그룹별로 시간 범위를 계산하여 수집/압축/업로드합니다.
"""

import sys
sys.path.append("/opt/layer")

import json
import logging
import time
from typing import Any, Dict

from modules import BatchProcessor, Config, S3StateManager


def setup_logging() -> None:
    """Lambda 환경에서 로깅 설정을 초기화합니다."""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 기존 핸들러 제거 (중복 방지)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(name)-32s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # 제3자 라이브러리 로깅 레벨 하향
    logging.getLogger('boto3').setLevel(logging.WARNING)
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def validate_config() -> bool:
    """환경변수 및 S3 상태 관리를 검증합니다."""
    logger = logging.getLogger(__name__)

    # S3 설정 검증
    if not Config.validate_s3_config():
        raise ValueError("S3 버킷 이름이 올바르게 설정되지 않았습니다.")

    # S3 상태 관리 사용 시 접근성 검증
    if Config.USE_S3_STATE:
        try:
            s3_state_manager = S3StateManager()
            if not s3_state_manager.validate_s3_access():
                logger.warning("S3 상태 관리 접근 권한 확인 실패.")
                return False
        except Exception as e:
            logger.error("S3 상태 관리 초기화 실패: %s", e)
            return False

    # 배치 설정 확인
    configs = Config.get_batch_configs()
    if not configs:
        logger.warning("배치 설정이 없습니다. 환경변수를 확인하세요.")
        return False

    logger.info("발견된 배치 설정: %d개", len(configs))
    for i, cfg in enumerate(configs, 1):
        logger.info("설정 %d: %s - %s", i, cfg['log_group_name'], cfg['stream_prefixes'])

    return True


def run_test_mode(event: Dict[str, Any]) -> Dict[str, Any]:
    """테스트 모드: 정규식 패턴 매칭 테스트만 수행"""
    logger = logging.getLogger(__name__)

    try:
        # 설정 검증
        if not validate_config():
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': '테스트 모드: 처리할 로그그룹이 없습니다.',
                    'mode': 'test',
                    'results': []
                })
            }

        # 배치 설정 가져오기
        configs = Config.get_batch_configs()
        test_results = []

        for config in configs:
            log_group_name = config['log_group_name']
            stream_prefixes = config['stream_prefixes']

            logger.info(f"🔍 테스트: {log_group_name} - {stream_prefixes}")

            # CloudWatch Log Reader 초기화
            from modules.cloudwatch_log_reader import CloudWatchLogReader
            reader = CloudWatchLogReader()

            # 각 스트림 접두사별로 테스트
            for stream_prefix in stream_prefixes:
                logger.info(f"  📋 스트림 접두사 테스트: {stream_prefix}")

                try:
                    # 로그스트림 목록 가져오기 (시간 범위 없이)
                    streams = reader.get_log_streams(log_group_name, stream_prefix)

                    # 매칭된 스트림들 로깅
                    matching_streams = []
                    for stream in streams:
                        stream_name = stream['logStreamName']
                        matching_streams.append({
                            'stream_name': stream_name,
                            'last_event_time': stream.get('lastEventTime'),
                            'stored_bytes': stream.get('storedBytes', 0)
                        })

                    test_results.append({
                        'log_group_name': log_group_name,
                        'stream_prefix': stream_prefix,
                        'matching_count': len(matching_streams),
                        'matching_streams': matching_streams,
                        'success': True
                    })

                    logger.info(f"    ✅ {stream_prefix}: {len(matching_streams)}개 스트림 매칭")
                    for stream_info in matching_streams[:5]:  # 처음 5개만 로깅
                        logger.info(
                            f"      - {stream_info['stream_name']} (마지막 이벤트: {stream_info['last_event_time']})")
                    if len(matching_streams) > 5:
                        logger.info(f"      ... 외 {len(matching_streams) - 5}개")

                except Exception as e:
                    logger.error(f"    ❌ {stream_prefix}: 테스트 실패 - {e}")
                    test_results.append({
                        'log_group_name': log_group_name,
                        'stream_prefix': stream_prefix,
                        'matching_count': 0,
                        'matching_streams': [],
                        'success': False,
                        'error': str(e)
                    })

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': '테스트 모드: 정규식 패턴 매칭 테스트 완료',
                'mode': 'test',
                'total_configs': len(configs),
                'results': test_results
            })
        }

    except Exception as e:
        logger.error(f"테스트 모드 실행 중 오류: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': '테스트 모드 실행 중 오류가 발생했습니다.',
                'mode': 'test',
                'error': str(e)
            })
        }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AWS Lambda 핸들러"""
    start_time_total = time.time()

    try:
        # 로깅 설정 및 기본 로그
        setup_logging()
        logger = logging.getLogger(__name__)

        # 테스트 모드 확인
        event_type = event.get('type', 'normal')
        if event_type == 'test':
            logger.info("🔍 테스트 모드로 실행됩니다. 실제 로그 수집은 수행하지 않습니다.")
            return run_test_mode(event)

        # 설정 검증
        if not validate_config():
            logger.info("처리할 로그그룹이 없거나 S3 상태 관리에 접근할 수 없습니다.")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': '처리할 로그그룹이 없거나 S3 상태 관리에 접근할 수 없습니다.',
                    'summary': {
                        'total_configs': 0,
                        'successful_count': 0,
                        'failed_count': 0,
                        'total_logs': 0,
                        'execution_time_seconds': 0,
                    },
                    'results': []
                }),
            }

        # 배치 프로세서 초기화
        batch_processor = BatchProcessor()

        # 각 로그그룹별로 시간 범위를 계산하므로 None 전달
        results = batch_processor.process_all_log_groups(None, None)
        logger.info("배치 처리 완료: %d개 결과", len(results))

        if not results:
            logger.info("처리할 로그그룹이 없습니다.")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': '처리할 로그그룹이 없습니다.',
                    'summary': {
                        'total_configs': 0,
                        'successful_count': 0,
                        'failed_count': 0,
                        'total_logs': 0,
                        'execution_time_seconds': round(time.time() - start_time_total, 2),
                    },
                    'results': []
                }),
            }

        # 결과 요약
        successful_count = sum(1 for r in results if r.get('success'))
        total_logs = sum(r.get('logs_count', 0) for r in results)
        failed_count = len(results) - successful_count

        logger.info(
            "배치 처리 완료: %d개 성공, %d개 실패, 총 %d개 로그",
            successful_count,
            failed_count,
            total_logs,
        )

        execution_time = time.time() - start_time_total
        logger.info("전체 실행 시간: %.2f초", execution_time)

        if successful_count > 0:
            logger.info("CloudWatch Log to S3 배치 프로세스가 성공적으로 완료되었습니다.")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': '배치 로그 수집 및 S3 업로드 완료',
                    'summary': {
                        'total_configs': len(results),
                        'successful_count': successful_count,
                        'failed_count': failed_count,
                        'total_logs': total_logs,
                        'execution_time_seconds': round(execution_time, 2),
                    },
                    'results': results,
                }),
            }

        logger.warning("모든 로그그룹 처리에 실패했습니다.")
        return {
            'statusCode': 400,
            'body': json.dumps({
                'message': '모든 로그그룹 처리에 실패했습니다.',
                'summary': {
                    'total_configs': len(results),
                    'successful_count': successful_count,
                    'failed_count': failed_count,
                    'total_logs': total_logs,
                    'execution_time_seconds': round(execution_time, 2),
                },
                'results': results
            }),
        }

    except Exception as e:
        # 예외 처리
        logging.getLogger(__name__).error("Lambda 함수 실행 중 오류 발생: %s", e)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e), 'message': 'Lambda 함수 실행 중 오류가 발생했습니다.'}),
        }


if __name__ == "__main__":
    # 로컬 테스트용
    setup_logging()
    result = lambda_handler({}, None)
    logging.getLogger(__name__).info(f"실행 결과: {json.dumps(result, ensure_ascii=False)}")
