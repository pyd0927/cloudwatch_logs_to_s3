# AWS CloudWatch Log to S3 (Lambda)

AWS CloudWatch Log의 특정 로그그룹에서 로그스트림을 읽어서 압축한 뒤 S3에 저장하는 AWS Lambda 함수입니다. **Retention 삭제 직전 구간을 백업**하기 위해 각 로그그룹별로 시간 범위를 계산하여 수집/압축/업로드합니다.

## 🚀 주요 기능

- **Retention 기반 시간 범위 계산**: CloudWatch Log Group의 Retention 설정을 기반으로 삭제 직전 구간 백업
- **상태 관리**: S3에 상태 파일을 저장하여 연속적인 로그 수집 및 누락 방지
- **배치 처리**: 여러 로그그룹을 동시에 처리
- **멀티스레딩**: Python ThreadPoolExecutor를 활용한 성능 최적화
- **로그 압축**: gzip 압축으로 저장 공간 절약
- **구조화된 S3 저장**: 날짜별 디렉토리 및 시간 범위 기반 파일명
- **밀리초 정확도**: S3 파일명에 밀리초 포함으로 정확한 시간 추적
- **정확한 패턴 매칭**: 길이 순 정렬과 매칭 추적으로 정확한 로그스트림 필터링
- **Container Insights 지원**: EKS Container Insights 로그 패턴 자동 인식
- **테스트 모드**: 정규식 패턴 매칭 테스트 기능

## 📋 요구사항

- AWS Lambda Python 3.9+
- CloudWatch Logs 및 S3 접근 권한
- EventBridge (CloudWatch Events) 정기 실행 설정

## 🛠️ 배포

### 1. IAM 정책 생성
```bash
aws iam create-policy \
    --policy-name CloudWatchLogToS3Policy \
    --policy-document file://iam_policy.json
```

### 2. IAM 역할 생성
```bash
aws iam create-role \
    --role-name CloudWatchLogToS3Role \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }'

aws iam attach-role-policy \
    --role-name CloudWatchLogToS3Role \
    --policy-arn arn:aws:iam::YOUR_ACCOUNT_ID:policy/CloudWatchLogToS3Policy
```

### 3. Lambda Layer 생성 및 연결
```bash
# Layer 배포 패키지 생성 (이미 준비된 layer.zip 사용)
# 또는 새로운 Layer 생성:
# mkdir -p layer/python
# pip install python-dateutil -t layer/python/
# zip -r layer.zip layer/

# Lambda Layer 생성
aws lambda publish-layer-version \
    --layer-name cloudwatch-log-to-s3-dependencies \
    --description "CloudWatch Log to S3 dependencies (python-dateutil)" \
    --zip-file fileb://layer.zip \
    --compatible-runtimes python3.9 \
    --compatible-architectures x86_64

# Layer ARN 저장 (출력에서 확인)
LAYER_ARN="arn:aws:lambda:REGION:ACCOUNT:layer:cloudwatch-log-to-s3-dependencies:VERSION"
```

### 4. Lambda 함수 생성
```bash
# 배포 패키지 생성
zip -r lambda_deployment.zip . -x "*.git*" "*.venv*" "test_*" "*.pyc" "README.md" "layer*"

# Lambda 함수 생성 (Layer 포함)
aws lambda create-function \
    --function-name cloudwatch-log-to-s3 \
    --runtime python3.9 \
    --role arn:aws:iam::YOUR_ACCOUNT_ID:role/CloudWatchLogToS3Role \
    --handler lambda_function.lambda_handler \
    --zip-file fileb://lambda_deployment.zip \
    --timeout 300 \
    --memory-size 512 \
    --layers $LAYER_ARN \
    --environment Variables='{
        "S3_BUCKET_NAME":"your-s3-bucket-name",
        "REGION":"ap-northeast-2",
        "LOG_GROUP_NAME_1":"/aws/eks/cluster-name/cluster",
        "LOG_STREAM_PREFIX_1":"kube-apiserver,kube-scheduler,kube-apiserver-audit,kube-controller-manager,authenticator",
        "LOG_GROUP_RETENTION_DAY_1":"1",
        "MINUTES_BACK":"30",
        "MAX_WORKERS":"4"
    }'
```

### 5. EventBridge 규칙 생성 (정기 실행)
```bash
aws events put-rule \
    --name cloudwatch-log-to-s3-schedule \
    --schedule-expression "rate(15 minutes)" \
    --description "CloudWatch Log to S3 배치 처리"

aws events put-targets \
    --rule cloudwatch-log-to-s3-schedule \
    --targets "Id"="1","Arn"="arn:aws:lambda:REGION:ACCOUNT:function:cloudwatch-log-to-s3"

# Lambda 권한 추가
aws lambda add-permission \
    --function-name cloudwatch-log-to-s3 \
    --statement-id EventBridgeInvoke \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:REGION:ACCOUNT:rule/cloudwatch-log-to-s3-schedule
```

## ⚙️ 환경변수 설정

### 필수 설정
```bash
# S3 버킷 이름
S3_BUCKET_NAME="s3-bucket-name"

# AWS 리전
REGION="ap-northeast-2"
```

### 배치 처리 설정 (여러 로그그룹)
```bash
# 첫 번째 로그그룹 (EKS 클러스터 로그)
LOG_GROUP_NAME_1="/aws/eks/cluster-name/cluster"
LOG_STREAM_PREFIX_1="kube-apiserver,kube-scheduler,kube-apiserver-audit,kube-controller-manager,authenticator"
LOG_GROUP_RETENTION_DAY_1="1"  # Retention 설정에서 1일을 뺀 범위로 로그 수집

# 두 번째 로그그룹 (Container Insights)
LOG_GROUP_NAME_2="/aws/containerinsights/cluster-name/dataplane"
LOG_STREAM_PREFIX_2="kube-proxy,kube-system"
LOG_GROUP_RETENTION_DAY_2="2"  # Retention 설정에서 2일을 뺀 범위로 로그 수집

# 세 번째 로그그룹 (Retention 설정 없음)
LOG_GROUP_NAME_3="/aws/lambda/your-function"
LOG_STREAM_PREFIX_3="2024"
# LOG_GROUP_RETENTION_DAY_3 설정하지 않으면 기본 MINUTES_BACK 사용
```

### 선택적 설정
```bash
# 시간 범위 설정 (분 단위)
MINUTES_BACK="30"  # 기본값: 30분

# 멀티스레딩 워커 수
MAX_WORKERS="4"  # 기본값: 4

# S3 상태 관리 사용 여부
USE_S3_STATE="true"  # 기본값: true
```

## 🔄 Retention 기반 시간 범위 계산

### 동작 방식
1. **CloudWatch Log Group의 Retention 설정 조회**
2. **실제 Retention 삭제 시점 계산**: `현재시간 - (Retention일수 - LOG_GROUP_RETENTION_DAY_X)`
3. **로그 수집 범위**: `삭제시점 - MINUTES_BACK` ~ `삭제시점`

### 예시
- **Retention 설정**: 7일
- **LOG_GROUP_RETENTION_DAY_1**: 1
- **MINUTES_BACK**: 30분
- **현재 시간**: 2025-01-20 10:00:00

**계산 과정**:
- `effective_retention_days = 7 - 1 = 6일`
- `retention_deletion_time = 2025-01-20 10:00:00 - 6일 = 2025-01-14 10:00:00`
- **수집 범위**: `2025-01-14 09:30:00 ~ 2025-01-14 10:00:00`

### 연속 실행 시 동작
- **첫 번째 실행**: `09:30:00 ~ 10:00:00` → `last_read_time = 10:00:00`
- **두 번째 실행**: `10:00:00 ~ 10:30:00` → `last_read_time = 10:30:00`
- **결과**: 연속적으로 30분씩 로그 수집

## 🎯 정확한 패턴 매칭

### 길이 순 정렬 매칭
로그스트림 접두사를 길이 순으로 정렬하여 더 정확한 매칭을 수행합니다:

```bash
# 처리 순서 (긴 접두사부터)
1. kube-controller-manager (길이: 22)
2. kube-apiserver-audit (길이: 20)
3. kube-apiserver (길이: 15)
4. kube-scheduler (길이: 15)
5. authenticator (길이: 13)
```

### 매칭 추적 시스템
이미 매칭된 스트림을 추적하여 중복 매칭을 방지합니다:

```
kube-apiserver-audit-xxx → kube-apiserver-audit 접두사와 매칭
kube-apiserver-xxx → kube-apiserver 접두사와 매칭 (audit 스트림은 제외됨)
```

### Container Insights 패턴 지원
EKS Container Insights의 복잡한 로그스트림 패턴을 자동으로 인식합니다:

```
# 일반 패턴
kube-proxy-abc123

# Container Insights 패턴
ip-10-0-3-92.ap-northeast-2.compute.internal-dataplane.tail.var.log.containers.kube-proxy-h6msz_kube-system_kube-proxy-...
```

## 📁 S3 저장 구조

### 파일명 형식 (밀리초 포함)
```
CloudWatchLogs/로그그룹명/로그스트림명/YYYY/MM/DD/YYYYMMDD_HHMMSS_MMM_YYYYMMDD_HHMMSS_MMM.log.gz
```

### 예시
```
CloudWatchLogs/_aws_eks_cluster-name_cluster/kube-apiserver/2025/01/20/20250120_093000_123_20250120_100000_456.log.gz
```

### 디렉토리 구조
```
CloudWatchLogs/
├── _aws_eks_cluster-name_cluster/
│   ├── kube-apiserver/
│   │   └── 2025/01/20/
│   │       ├── 20250120_093000_123_20250120_100000_456.log.gz
│   │       └── 20250120_100000_456_20250120_103000_789.log.gz
│   ├── kube-apiserver-audit/
│   │   └── 2025/01/20/
│   │       └── 20250120_093000_123_20250120_100000_456.log.gz
│   └── kube-scheduler/
│       └── 2025/01/20/
│           └── 20250120_093000_123_20250120_100000_456.log.gz
└── CloudWatchLogsState/
    └── state.json
```

## 🔧 Lambda 함수 사용법

### 일반 실행
EventBridge 규칙에 의해 자동으로 10분마다 실행됩니다.

### 수동 실행
```bash
# Lambda 함수 직접 호출
aws lambda invoke \
    --function-name cloudwatch-log-to-s3 \
    --payload '{}' \
    response.json
```

### 테스트 모드 실행
정규식 패턴 매칭을 테스트할 수 있습니다:

```bash
# 테스트 모드로 실행 (실제 로그 수집 없음)
aws lambda invoke \
    --function-name cloudwatch-log-to-s3 \
    --payload '{"type": "test"}' \
    test-response.json

# 결과 확인
cat test-response.json
```

## 🔍 상태 관리

### S3 상태 파일 구조
```json
{
  "/aws/eks/cluster-name/cluster": {
    "log_group_name": "/aws/eks/cluster-name/cluster",
    "last_run_time": "2025-01-20T10:00:00.000000+00:00",
    "last_read_time": "2025-01-14T10:00:00.000000+00:00",
    "streams": {
      "kube-apiserver-abc123": {
        "next_token": "f/1234567890...",
        "last_event_time": 1705233600000,
        "last_updated": "2025-01-20T10:00:00.000000+00:00"
      }
    },
    "created_at": "2025-01-20T09:30:00.000000+00:00",
    "state_version": "2.0"
  }
}
```

### 상태 관리 기능
- **연속 읽기**: `last_read_time`을 기준으로 이어서 읽기
- **스트림별 추적**: 각 로그스트림의 마지막 이벤트 시간 및 토큰 관리

### 중요
- 실행주기가 설정된 시간범위를 넘어선 경우 `last_read_time`을 무시하여 현재 실행 시간 기준으로 Retention 삭제 시점 재계산
### 예시
- **MINUTES_BACK**: 30분
- **마지막 실행**: 1시간 전
- 현재 시간 기준으로 Retention 삭제 시점부터 30분 전까지 로그 수집

## 🔧 문제 해결

### 일반적인 문제
1. **권한 오류**: IAM 정책 확인
2. **S3 접근 실패**: 버킷 이름 및 권한 확인
3. **로그 수집 실패**: 로그그룹 이름 및 스트림 접두사 확인
4. **상태 파일 오류**: S3 상태 파일 권한 확인
5. **Layer 오류**: Layer ARN 및 버전 확인

### 디버깅
```bash
# Lambda 함수 로그 확인
aws logs tail /aws/lambda/cloudwatch-log-to-s3 --follow

# Lambda 함수 설정 확인 (Layer 포함)
aws lambda get-function --function-name cloudwatch-log-to-s3

# Layer 정보 확인
aws lambda list-layer-versions --layer-name cloudwatch-log-to-s3-dependencies

# S3 상태 파일 확인
aws s3 cp s3://your-bucket/CloudWatchLogsState/state.json -

# CloudWatch Logs 확인
aws logs describe-log-groups --log-group-name-prefix "/aws/eks"

# S3 업로드된 파일 확인
aws s3 ls s3://your-bucket/CloudWatchLogs/ --recursive

# 테스트 모드로 패턴 매칭 확인
aws lambda invoke \
    --function-name cloudwatch-log-to-s3 \
    --payload '{"type": "test"}' \
    test-response.json
```

## 🆕 최신 업데이트

### v1.1.0 (2025-07-04)
- **Lambda Layer 지원**: python-dateutil 의존성을 Layer로 분리
- **밀리초 정확도**: S3 파일명에 밀리초 포함
- **정확한 패턴 매칭**: 길이 순 정렬과 매칭 추적 시스템
- **Container Insights 지원**: EKS Container Insights 로그 패턴 자동 인식
- **테스트 모드**: 정규식 패턴 매칭 테스트 기능 추가
- **성능 최적화**: 중복 매칭 방지로 처리 속도 향상

## 📝 라이선스

MIT License

## 🤝 기여

버그 리포트 및 기능 요청은 GitHub Issues를 통해 제출해주세요.