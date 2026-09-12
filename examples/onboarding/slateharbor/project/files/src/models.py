"""Slateharbor files: synthetic local policy sample, no external services."""
from dataclasses import dataclass

@dataclass(frozen=True)
class UploadMbRequest:
    """file upload limit: 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다."""
    size_mb: int
    environment: str = 'production'

@dataclass(frozen=True)
class SignedUrlMinutesRequest:
    """signed URL lifetime: 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다."""
    age_minutes: int
    environment: str = 'production'

@dataclass(frozen=True)
class RetentionDaysRequest:
    """trash retention: 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다."""
    deleted_days: int
    environment: str = 'production'

@dataclass(frozen=True)
class ScanSecondsRequest:
    """malware scan timeout: 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다."""
    elapsed_seconds: int
    environment: str = 'production'

@dataclass(frozen=True)
class PreviewPagesRequest:
    """document preview: 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다."""
    pages: int
    environment: str = 'production'

@dataclass(frozen=True)
class ChunkMbRequest:
    """multipart chunk: 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다."""
    part_mb: int
    environment: str = 'production'

