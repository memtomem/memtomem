"""Slateharbor files: synthetic local policy sample, no external services."""
from files.src import policy

def summarize_upload_mb(observations, config):
    """file upload limit: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.upload_mb(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_signed_url_minutes(observations, config):
    """signed URL lifetime: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.signed_url_minutes(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_retention_days(observations, config):
    """trash retention: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.retention_days(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_scan_seconds(observations, config):
    """malware scan timeout: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.scan_seconds(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_preview_pages(observations, config):
    """document preview: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.preview_pages(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

def summarize_chunk_mb(observations, config):
    """multipart chunk: count permitted observations, never turn absence into success."""
    values = list(observations)
    if not values:
        return {'state': 'unknown', 'total': 0}
    accepted = sum(bool(policy.chunk_mb(value, config)) for value in values)
    return {'state': 'observed', 'total': len(values), 'accepted': accepted, 'rejected': len(values) - accepted}

