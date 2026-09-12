"""Slateharbor files: synthetic local policy sample, no external services."""
from files.src import policy

def replay_upload_mb(fixtures, config):
    """Replay file upload limit input examples; no real requests are sent."""
    case = fixtures['upload_mb']
    return [policy.upload_mb(value, config) for value in case['inputs']]

def replay_signed_url_minutes(fixtures, config):
    """Replay signed URL lifetime input examples; no real requests are sent."""
    case = fixtures['signed_url_minutes']
    return [policy.signed_url_minutes(value, config) for value in case['inputs']]

def replay_retention_days(fixtures, config):
    """Replay trash retention input examples; no real requests are sent."""
    case = fixtures['retention_days']
    return [policy.retention_days(value, config) for value in case['inputs']]

def replay_scan_seconds(fixtures, config):
    """Replay malware scan timeout input examples; no real requests are sent."""
    case = fixtures['scan_seconds']
    return [policy.scan_seconds(value, config) for value in case['inputs']]

def replay_preview_pages(fixtures, config):
    """Replay document preview input examples; no real requests are sent."""
    case = fixtures['preview_pages']
    return [policy.preview_pages(value, config) for value in case['inputs']]

def replay_chunk_mb(fixtures, config):
    """Replay multipart chunk input examples; no real requests are sent."""
    case = fixtures['chunk_mb']
    return [policy.chunk_mb(value, config) for value in case['inputs']]

