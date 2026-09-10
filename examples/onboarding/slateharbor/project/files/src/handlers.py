"""Slateharbor files: synthetic local policy sample, no external services."""
from files.src import policy

def evaluate_upload_mb(size_mb, config):
    """content length와 실제 수신량을 비교하고 multipart upload 상태를 조회한다."""
    return {'policy': 'upload_mb', 'allowed': policy.upload_mb(size_mb, config),
            'revision': config['upload_mb']['revision'], 'observed': size_mb}

def evaluate_signed_url_minutes(age_minutes, config):
    """링크 발급 시각과 공유 해제 시각을 비교하고 신규 URL 발급을 중단한다."""
    return {'policy': 'signed_url_minutes', 'allowed': policy.signed_url_minutes(age_minutes, config),
            'revision': config['signed_url_minutes']['revision'], 'observed': age_minutes}

def evaluate_retention_days(deleted_days, config):
    """삭제 표시와 실제 blob 존재를 확인하고 hold 여부부터 조회한다."""
    return {'policy': 'retention_days', 'allowed': policy.retention_days(deleted_days, config),
            'revision': config['retention_days']['revision'], 'observed': deleted_days}

def evaluate_scan_seconds(elapsed_seconds, config):
    """scan 상태가 pending인지 확인하고 재검사 큐로 보낸다."""
    return {'policy': 'scan_seconds', 'allowed': policy.scan_seconds(elapsed_seconds, config),
            'revision': config['scan_seconds']['revision'], 'observed': elapsed_seconds}

def evaluate_preview_pages(pages, config):
    """쪽 수와 preview job 종류를 확인하고 대형 변환을 별도 큐로 보낸다."""
    return {'policy': 'preview_pages', 'allowed': policy.preview_pages(pages, config),
            'revision': config['preview_pages']['revision'], 'observed': pages}

def evaluate_chunk_mb(part_mb, config):
    """실패한 part 번호와 checksum을 확인하고 해당 조각만 재전송한다."""
    return {'policy': 'chunk_mb', 'allowed': policy.chunk_mb(part_mb, config),
            'revision': config['chunk_mb']['revision'], 'observed': part_mb}

