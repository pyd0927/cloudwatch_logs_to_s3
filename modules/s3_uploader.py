import boto3
import logging
from datetime import datetime, timedelta, timezone
from .config import Config


class S3Uploader:
    """S3에 압축된 로그 파일을 업로드하는 클래스"""

    def __init__(self, region_name: str = None):
        self.region_name = region_name or Config.REGION
        self.s3_client = boto3.client('s3', region_name=self.region_name)
        self.logger = logging.getLogger(__name__)

    def upload_compressed_logs(
        self, bucket_name: str, s3_key: str,
        compressed_data: bytes,
        content_type: str = 'application/octet-stream'
    ) -> bool:
        """압축된 로그 데이터를 S3에 업로드합니다."""
        try:
            # 압축 데이터 검증
            if not compressed_data:
                self.logger.error("업로드할 압축 데이터가 비어있습니다.")
                return False

            # gzip 헤더 확인
            if not compressed_data.startswith(b'\x1f\x8b'):
                self.logger.error(f"업로드할 데이터가 gzip 압축되지 않았습니다! (데이터 헤더: {compressed_data[:10].hex()})")
                return False

            # 브라우저에서 자동 압축 해제를 방지하기 위한 메타데이터 설정
            metadata = {
                'original-format': 'json',
                'compression': 'gzip',
                'compression-ratio': f"{len(compressed_data)}",
                'created-by': 'cloudwatch-log-to-s3'
            }

            self.s3_client.put_object(
                Bucket=bucket_name,
                Key=s3_key,
                Body=compressed_data,
                ContentType=content_type,  # application/octet-stream으로 변경
                # ContentEncoding 제거 (브라우저 자동 압축 해제 방지)
                ServerSideEncryption='AES256',
                Metadata=metadata
            )

            self.logger.info(f"S3 업로드 성공: {s3_key} ({len(compressed_data)} bytes, gzip 확인됨)")
            return True

        except Exception as e:
            self.logger.error(f"S3 업로드 실패: {s3_key}, 오류: {e}")
            import traceback
            self.logger.error(f"스택 트레이스: {traceback.format_exc()}")
            return False

    def check_bucket_exists(self, bucket_name: str) -> bool:
        """S3 버킷이 존재하는지 확인합니다."""
        try:
            self.s3_client.head_bucket(Bucket=bucket_name)
            return True
        except Exception as e:
            self.logger.error(f"버킷 확인 실패: {e}")
            return False

    def list_uploaded_files(self, bucket_name: str, prefix: str = None) -> list:
        """업로드된 파일 목록을 가져옵니다."""
        try:
            params = {'Bucket': bucket_name}
            if prefix:
                params['Prefix'] = prefix

            response = self.s3_client.list_objects_v2(**params)
            return response.get('Contents', [])

        except Exception as e:
            self.logger.error(f"파일 목록 가져오기 실패: {e}")
            return []

    def delete_old_files(self, bucket_name: str, prefix: str, days_to_keep: int = 30) -> int:
        """오래된 파일을 삭제합니다."""
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
            deleted_count = 0

            files = self.list_uploaded_files(bucket_name, prefix)

            for file_obj in files:
                if file_obj['LastModified'] < cutoff_date:
                    self.s3_client.delete_object(
                        Bucket=bucket_name,
                        Key=file_obj['Key']
                    )
                    deleted_count += 1
                    self.logger.info(f"오래된 파일 삭제: {file_obj['Key']}")

            self.logger.info(f"총 {deleted_count}개의 오래된 파일을 삭제했습니다.")
            return deleted_count

        except Exception as e:
            self.logger.error(f"오래된 파일 삭제 실패: {e}")
            return 0
