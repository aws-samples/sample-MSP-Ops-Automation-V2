"""
Deterministic guard policies for remediation step validation.

Runs BEFORE the LLM guard — instant, deterministic, zero-cost.
Blocks dangerous operations regardless of LLM judgment.

Two-tier guard design:
  Tier 1 (this module): Regex blocklist applied unconditionally.  No network call,
    no LLM cost.  Catches operations that should NEVER be automated regardless of
    environment or KB guidance (e.g., terminate-instances, delete-db-cluster).
  Tier 2 (workflow_graph.py Phase 3): LLM guard validates the approved set against
    KB context.  Prompt strictness varies by GUARD_MODE (see below).

GUARD_MODE values (set via GUARD_MODE env var / config_loader.py):
  "demo"       — Tier 2 LLM guard is lenient; only rejects genuinely destructive
                 operations (data deletion, infrastructure teardown) so that realistic
                 demo scenarios work without false rejections.
  "production" — Tier 2 LLM guard enforces strict criteria: requires --dry-run for
                 EC2/RDS, rejects irreversible or high-blast-radius operations, and
                 blocks any IAM or network modifications.

Note: BLOCK_PATTERNS applies in both modes; PRODUCTION_REQUIRE only activates in
production mode.
"""

import re
import logging
from typing import List, Tuple, Union, Dict

logger = logging.getLogger(__name__)

# Regex patterns that are ALWAYS blocked regardless of GUARD_MODE.
# These cover destructive operations with no safe-to-automate use case:
# data deletion, instance termination, IAM privilege escalation, and
# VPC/network resource destruction.
BLOCK_PATTERNS = [
    r'\bdelete-db-instance\b',
    r'\bdelete-db-cluster\b',
    r'\bdelete-table\b',
    r'\bterminate-instances\b',
    r'\bdelete-stack\b',
    r'\bdelete-bucket\b',
    r'\bremove-role-policy\b',
    r'\bdelete-role\b',
    r'\bdelete-user\b',
    r'\bdelete-policy\b',
    r'\bput-user-policy\b',
    r'\battach-user-policy\b',
    r'\bcreate-access-key\b',
    r'\bdelete-security-group\b',
    r'\bdelete-subnet\b',
    r'\bdelete-vpc\b',
    r'\bformat-volume\b',
    r'\bdelete-volume\b',
    r'\bdelete-snapshot\b',
    r'\bdelete-cluster\b',
    r'\bderegister-task-definition\b',
    r'\bdelete-service\b',
    r'\bdelete-function\b',
    r'\bdelete-queue\b',
    r'\bdelete-topic\b',
]

# Production mode only: per-service required flags.
# If a command targets a service listed here, each flag in the list must appear
# in the command string.  Empty list means no extra flags required for that service
# (the service is still subject to BLOCK_PATTERNS).
PRODUCTION_REQUIRE = {
    "ec2": ["--dry-run"],
    "rds": ["--dry-run"],
    "ecs": [],
    "lambda": [],
    "s3": [],
    "dynamodb": [],
    "apigateway": [],
    "elbv2": [],
}


def validate_steps(steps: List[Union[str, Dict]], mode: str = "demo") -> Tuple[bool, str]:
    """
    Validate remediation steps against deterministic policies.

    Args:
        steps: List of CLI command strings or dicts with 'cli_command' key
        mode: 'demo' or 'production'

    Returns:
        (approved, reason) — approved=True if all steps pass
    """
    for step in steps:
        cmd = step if isinstance(step, str) else step.get("cli_command", "")
        cmd_lower = cmd.lower()

        # Check block patterns
        for pattern in BLOCK_PATTERNS:
            if re.search(pattern, cmd_lower):
                reason = f"Blocked by policy: '{pattern.strip(chr(92)).strip('b')}' found in: {cmd[:120]}"
                logger.warning(f"[GUARD_POLICY] {reason}")
                return False, reason

        # Production mode: enforce per-service required flags (e.g. --dry-run for EC2/RDS).
        # In demo mode this block is skipped entirely so demo workflows are not blocked
        # by safety flags that would require special IAM permissions or produce no-ops.
        if mode == "production":
            # Extract the service name from 'aws <service> <subcommand> ...'
            svc_match = re.match(r'aws\s+(\S+)', cmd_lower)
            if svc_match:
                service = svc_match.group(1)
                required_flags = PRODUCTION_REQUIRE.get(service, [])
                for flag in required_flags:
                    if flag not in cmd_lower:
                        reason = f"Production policy requires '{flag}' for {service} commands: {cmd[:120]}"
                        logger.warning(f"[GUARD_POLICY] {reason}")
                        return False, reason

    return True, "All steps passed policy validation"
