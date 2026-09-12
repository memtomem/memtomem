"""Slateharbor files: synthetic local policy sample, no external services."""
from copy import deepcopy

def configure_upload_mb(config, value):
    """Validate and copy file upload limit; production value is 25."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid upload_mb')
    updated = deepcopy(config)
    updated['upload_mb']['value'] = value
    return updated

def configure_signed_url_minutes(config, value):
    """Validate and copy signed URL lifetime; production value is 10."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid signed_url_minutes')
    updated = deepcopy(config)
    updated['signed_url_minutes']['value'] = value
    return updated

def configure_retention_days(config, value):
    """Validate and copy trash retention; production value is 30."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid retention_days')
    updated = deepcopy(config)
    updated['retention_days']['value'] = value
    return updated

def configure_scan_seconds(config, value):
    """Validate and copy malware scan timeout; production value is 120."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid scan_seconds')
    updated = deepcopy(config)
    updated['scan_seconds']['value'] = value
    return updated

def configure_preview_pages(config, value):
    """Validate and copy document preview; production value is 20."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid preview_pages')
    updated = deepcopy(config)
    updated['preview_pages']['value'] = value
    return updated

def configure_chunk_mb(config, value):
    """Validate and copy multipart chunk; production value is 8."""
    if type(value) is not int or value < 0:
        raise ValueError('Invalid chunk_mb')
    updated = deepcopy(config)
    updated['chunk_mb']['value'] = value
    return updated

