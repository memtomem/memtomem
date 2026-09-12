"""Slateharbor jobs: synthetic local policy sample, no external services."""
from jobs.src import policy

def evaluate_lease_seconds(heartbeat_age, config):
    """checkpoint와 lease owner를 함께 읽는다. 완료면 결과를 재사용하고 만료·미완료일 때만 다시 claim한다."""
    return {'policy': 'lease_seconds', 'allowed': policy.lease_seconds(heartbeat_age, config),
            'revision': config['lease_seconds']['revision'], 'observed': heartbeat_age}

def evaluate_max_parallel(running, config):
    """CPU와 heartbeat 지연을 함께 보고 동시 작업 수를 낮춘다."""
    return {'policy': 'max_parallel', 'allowed': policy.max_parallel(running, config),
            'revision': config['max_parallel']['revision'], 'observed': running}

def evaluate_checkpoint_seconds(elapsed_seconds, config):
    """출력 flush와 checkpoint 순서를 확인하고 마지막 durable offset부터 재개한다."""
    return {'policy': 'checkpoint_seconds', 'allowed': policy.checkpoint_seconds(elapsed_seconds, config),
            'revision': config['checkpoint_seconds']['revision'], 'observed': elapsed_seconds}

def evaluate_retry_budget(attempts, config):
    """입력 checksum과 실패 원인이 같은지 확인하고 재현 자료를 격리한다."""
    return {'policy': 'retry_budget', 'allowed': policy.retry_budget(attempts, config),
            'revision': config['retry_budget']['revision'], 'observed': attempts}

def evaluate_shutdown_seconds(elapsed_seconds, config):
    """draining 상태와 마지막 checkpoint를 확인한 뒤 프로세스를 종료한다."""
    return {'policy': 'shutdown_seconds', 'allowed': policy.shutdown_seconds(elapsed_seconds, config),
            'revision': config['shutdown_seconds']['revision'], 'observed': elapsed_seconds}

def evaluate_backlog_limit(queued, config):
    """tenant별 입장 제한을 확인하고 예약 작업의 backpressure를 켠다."""
    return {'policy': 'backlog_limit', 'allowed': policy.backlog_limit(queued, config),
            'revision': config['backlog_limit']['revision'], 'observed': queued}

