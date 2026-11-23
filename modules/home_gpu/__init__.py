"""Home-GPU batch jobs over the shared mailbox (#743, part 1: #759, part 2a: #812).

Nexus only *submits* jobs here: a ``batch_job`` file addressed ``to: home-gpu``
in the mailbox inbox, plus a ``queued`` row in the run store's
``home_gpu_jobs`` table written only after the file is committed. The home
PC's own worker (not part of this repo) picks the files up. Everything is
behind ``NEXUS_HOME_GPU=1`` and off by default.
"""

from .jobs import (
    ALLOWED_KINDS,
    MAX_PAYLOAD_BYTES,
    HomeGpuDisabled,
    HomeGpuJobError,
    HomeGpuSubmitError,
    PayloadRejected,
    SubmittedJob,
    check_payload,
    home_gpu_enabled,
    submit_job,
)

__all__ = [
    "ALLOWED_KINDS",
    "MAX_PAYLOAD_BYTES",
    "HomeGpuDisabled",
    "HomeGpuJobError",
    "HomeGpuSubmitError",
    "PayloadRejected",
    "SubmittedJob",
    "check_payload",
    "home_gpu_enabled",
    "submit_job",
]
