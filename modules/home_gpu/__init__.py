"""Home-GPU batch jobs over the shared mailbox (#743).

Part 1 (#759): Nexus only *submits* jobs here - a ``batch_job`` file addressed
``to: home-gpu`` in the mailbox inbox; the home PC's own worker (not part of
this repo) picks them up. Part 2 (#813): replies are collected from the outbox.
Part 3 (#761): deadline expiry, PC-off detection with fallback to the VM local
path, and the savings summary shown under ``/status``. Everything is behind
``NEXUS_HOME_GPU=1`` and off by default.

The canonical submission entry point is :func:`submit_job` - the fallback
wrapper from :mod:`modules.home_gpu.fallback`, which tracks the job in
``home_gpu_jobs`` and diverts to the VM path when the PC counts as off. The
mailbox-only writer stays available as :func:`submit_mailbox_job`.
"""

from .collect import collect_replies
from .fallback import (
    BATCH_KINDS,
    FALLBACK,
    INTERACTIVE_KINDS,
    STATUS_EXPIRED,
    STATUS_FALLBACK,
    STATUS_HOME_GPU_EXPIRED,
    enqueue_vm_local,
    expire_jobs,
    home_gpu_status,
    in_vm_window,
    pc_is_off,
    pc_probe_interval,
    savings_summary,
    submit_job,
    vm_window,
    vm_window_end,
    vm_window_info,
)
from .jobs import (
    ALLOWED_KINDS,
    MAX_PAYLOAD_BYTES,
    HomeGpuDisabled,
    HomeGpuJobError,
    PayloadRejected,
    SubmittedJob,
    check_payload,
    home_gpu_enabled,
    payload_body,
    submit_job as submit_mailbox_job,
)

__all__ = [
    "ALLOWED_KINDS",
    "BATCH_KINDS",
    "FALLBACK",
    "INTERACTIVE_KINDS",
    "MAX_PAYLOAD_BYTES",
    "STATUS_EXPIRED",
    "STATUS_FALLBACK",
    "STATUS_HOME_GPU_EXPIRED",
    "HomeGpuDisabled",
    "HomeGpuJobError",
    "PayloadRejected",
    "SubmittedJob",
    "check_payload",
    "collect_replies",
    "enqueue_vm_local",
    "expire_jobs",
    "home_gpu_enabled",
    "home_gpu_status",
    "in_vm_window",
    "payload_body",
    "pc_is_off",
    "pc_probe_interval",
    "savings_summary",
    "submit_job",
    "submit_mailbox_job",
    "vm_window",
    "vm_window_end",
    "vm_window_info",
]
