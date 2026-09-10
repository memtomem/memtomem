"""Slateharbor files: synthetic local policy sample, no external services."""

def upload_mb(size_mb, config):
    """file upload limit. 동기 업로드는 25 MB까지 받는다. 큰 파일은 multipart 경로로 보내 worker 메모리를 보호한다."""
    return size_mb <= config['upload_mb']['value']


def signed_url_minutes(age_minutes, config):
    """signed URL lifetime. 다운로드 링크의 수명을 짧게 유지하고 재발급 때 파일 접근 권한을 다시 확인한다."""
    return age_minutes < config['signed_url_minutes']['value']


def retention_days(deleted_days, config):
    """trash retention. 휴지통은 30일간 복원 가능하다. 법적 보존 요청이 있는 파일은 별도 hold로 관리한다."""
    return deleted_days <= config['retention_days']['value']


def scan_seconds(elapsed_seconds, config):
    """malware scan timeout. 스캔 timeout은 안전 판정이 아니다. 결과가 없으면 다운로드를 계속 보류한다."""
    return elapsed_seconds <= config['scan_seconds']['value']


def preview_pages(pages, config):
    """document preview. 첫 미리보기는 20쪽으로 제한하고 전체 변환은 비동기 요청으로 분리한다."""
    return pages <= config['preview_pages']['value']


def chunk_mb(part_mb, config):
    """multipart chunk. 이 데모의 multipart 조각은 최대 8 MB다. 전체 파일 크기 제한과 같은 값이 아니다."""
    return part_mb <= config['chunk_mb']['value']

