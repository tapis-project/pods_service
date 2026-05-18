"""
Utilities for pod log run archival, retention enforcement, and size management.

Config (via tapisservice.config.conf):
  log_archive_path   — root directory for gzip archives (default: /tmp/pods/log-archives)
  log_max_size_bytes — max byte size of a single run's logs before truncation (default: 10MB)
  log_runs_to_keep   — number of most-recent runs to keep active in the DB (default: 3)
"""
import gzip
import os
from datetime import datetime
from typing import TYPE_CHECKING

from tapisservice.config import conf
from tapisservice.logs import get_logger

if TYPE_CHECKING:
    from models_pod_log_runs import PodLogRun

logger = get_logger(__name__)

LOG_ARCHIVE_ROOT: str = conf.get('log_archive_path', '/tmp/pods/log-archives')
LOG_MAX_SIZE_BYTES: int = int(conf.get('log_max_size_bytes', 10 * 1024 * 1024))  # 10 MB
RUNS_TO_KEEP: int = int(conf.get('log_runs_to_keep', 3))
# Hard cap on total runs kept per pod (active + archived in DB) before hard-deleting
_RUNS_HARD_CAP: int = RUNS_TO_KEEP + 10


def _archive_path_for(tenant_id: str, pod_id: str, run_index: int) -> str:
    return os.path.join(LOG_ARCHIVE_ROOT, tenant_id, pod_id, f"run_{run_index}.log.gz")


def archive_run(run: 'PodLogRun') -> str:
    """Gzip the run's logs to disk and return the archive path.

    Creates parent directories as needed. Safe to call even if logs is None.
    """
    path = _archive_path_for(run.tenant_id, run.pod_id, run.run_index)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (run.logs or '').encode('utf-8', errors='replace')
    with gzip.open(path, 'wb') as f:
        f.write(content)
    logger.info(f"Archived pod {run.pod_id} run #{run.run_index} → {path} ({len(content)} bytes)")
    return path


def read_archive(archive_path: str) -> str:
    """Decompress and return the contents of a .log.gz archive file."""
    with gzip.open(archive_path, 'rb') as f:
        return f.read().decode('utf-8', errors='replace')


def purge_archived_run(run: 'PodLogRun'):
    """Delete the on-disk archive file (if present) and the DB row."""
    if run.archive_path and os.path.exists(run.archive_path):
        try:
            os.remove(run.archive_path)
            logger.info(f"Deleted archive file {run.archive_path}")
        except OSError as e:
            logger.warning(f"Could not delete archive {run.archive_path}: {e}")
    run.db_delete(tenant=run.tenant_id, site=run.site_id)


def truncate_if_oversized(logs: str) -> str:
    """If logs exceed LOG_MAX_SIZE_BYTES, drop lines from the beginning until under the cap."""
    if not logs:
        return logs
    encoded = logs.encode('utf-8', errors='replace')
    if len(encoded) <= LOG_MAX_SIZE_BYTES:
        return logs
    # Drop oldest lines until we're under the cap
    lines = logs.splitlines(keepends=True)
    while lines and len(''.join(lines).encode('utf-8', errors='replace')) > LOG_MAX_SIZE_BYTES:
        lines.pop(0)
    truncated = ''.join(lines)
    logger.info(f"Truncated logs to {len(truncated.encode())} bytes (cap {LOG_MAX_SIZE_BYTES})")
    return truncated


def maybe_archive_old_runs(pod_id: str, tenant: str, site: str):
    """Enforce run retention for a pod.

    - If active run count > RUNS_TO_KEEP: archive the oldest active run and mark it inactive.
    - If total run count > _RUNS_HARD_CAP: hard-delete the oldest archived runs.
    """
    from models_pod_log_runs import PodLogRun

    active_runs = PodLogRun.get_active_runs(pod_id, tenant, site)
    # active_runs is ordered by run_index DESC; oldest is last
    while len(active_runs) > RUNS_TO_KEEP:
        oldest = active_runs[-1]
        try:
            archive_path = archive_run(oldest)
            oldest.is_active = False
            oldest.is_archived = True
            oldest.archive_path = archive_path
            oldest.logs = None  # clear from DB after archiving
            oldest.db_update(tenant=tenant, site=site)
            logger.info(f"Pod {pod_id}: archived run #{oldest.run_index}, {RUNS_TO_KEEP} active kept.")
        except Exception as e:
            logger.error(f"Failed to archive run #{oldest.run_index} for pod {pod_id}: {e}")
        active_runs = PodLogRun.get_active_runs(pod_id, tenant, site)

    # Hard-delete runs beyond the cap (oldest archived ones)
    all_runs = PodLogRun.list_runs(pod_id, tenant, site)  # DESC order
    if len(all_runs) > _RUNS_HARD_CAP:
        to_delete = all_runs[_RUNS_HARD_CAP:]  # oldest, beyond cap
        for run in to_delete:
            try:
                purge_archived_run(run)
                logger.info(f"Pod {pod_id}: hard-deleted run #{run.run_index} (beyond cap {_RUNS_HARD_CAP}).")
            except Exception as e:
                logger.error(f"Failed to hard-delete run #{run.run_index} for pod {pod_id}: {e}")
