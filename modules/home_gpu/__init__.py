"""Home-GPU batch jobs over the shared mailbox (#743, part 1: #759).

Nexus only *submits* jobs here: a ``batch_job`` file addressed ``to: home-gpu``
in the mailbox inbox. The home PC's own worker (not part of this repo) picks
them up. Everything is behind ``NEXUS_HOME_GPU=1`` and off by default.
"""

from .jobs import (
    ALLOWED_KINDS,
    MAX_PAYLOAD_BYTES,
    HomeGpuDisabled,
    HomeGpuJobError,
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
    "PayloadRejected",
    "SubmittedJob",
    "check_payload",
    "home_gpu_enabled",
    "submit_job",
]
