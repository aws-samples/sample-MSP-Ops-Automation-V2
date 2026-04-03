"""Shared credential helper for MCP servers.

Fetches per-customer AWS credentials stored in Secrets Manager under the path
``msp-credentials/{account_name}`` and constructs a boto3 Session for cross-account
access.  Credentials are automatically refreshed via STS AssumeRole when they are
within 10 minutes of expiry, and the refreshed values are written back to the secret
so subsequent calls reuse them without an extra AssumeRole round-trip.

Secret schema (JSON):
    aws_access_key_id     — IAM/STS access key
    aws_secret_access_key — IAM/STS secret key
    aws_session_token     — STS session token (optional for long-term keys)
    expires_at            — ISO-8601 UTC expiry timestamp (optional)
    role_arn              — IAM Role ARN to assume when refreshing (required for refresh)
    external_id           — STS ExternalId condition value (optional)
    customer_name         — Human-readable display name returned alongside the session

Usage:
    session, display_name = get_customer_session("acme-prod", region="us-east-1")
"""
import boto3
import json
import time
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def get_customer_session(account_name: str, region: str = "us-east-1") -> tuple:
    """Get boto3 session for customer account. Refreshes expired credentials.

    Args:
        account_name: Customer account name (key in msp-credentials/{name})
        region: AWS region

    Returns:
        (boto3.Session, display_name) or (None, None) if default/failed
    """
    if not account_name or account_name == "default":
        logger.info(f"credential_helper: account_name={account_name!r} → using default (no Secrets Manager lookup)")
        return None, None

    logger.info(f"credential_helper: account_name={account_name!r} → looking up msp-credentials/{account_name}")
    secrets_client = boto3.client('secretsmanager', region_name=region)
    secret_name = f"msp-credentials/{account_name}"

    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        creds = json.loads(response['SecretString'])
    except Exception as e:
        logger.error(f"Failed to get secret {secret_name}: {e}")
        return None, None

    # Check expiry — refresh if within 10 minutes
    expires_at = creds.get('expires_at', '')
    try:
        expiry = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        if expiry <= datetime.now(timezone.utc) + timedelta(minutes=10):
            logger.info(f"Credentials expired for {account_name}, refreshing...")
            creds = _refresh_credentials(secrets_client, secret_name, creds, region)
            if not creds:
                return None, None
    except (ValueError, TypeError):
        pass  # No valid expiry — use as-is

    session = boto3.Session(
        aws_access_key_id=creds['aws_access_key_id'],
        aws_secret_access_key=creds['aws_secret_access_key'],
        aws_session_token=creds.get('aws_session_token'),
        region_name=region
    )
    
    # Verify identity and log for debugging
    try:
        sts = session.client('sts')
        identity = sts.get_caller_identity()
        logger.info(f"✅ Assumed customer session for '{account_name}':")
        logger.info(f"   Account: {identity['Account']}")
        logger.info(f"   Role ARN: {identity['Arn']}")
        logger.info(f"   User ID: {identity['UserId']}")
    except Exception as id_err:
        logger.warning(f"⚠️  Could not verify identity for '{account_name}': {id_err}")
    
    return session, creds.get('customer_name', account_name)


def _refresh_credentials(secrets_client, secret_name, creds, region):
    """Refresh expired credentials via STS AssumeRole and persist them back to Secrets Manager.

    Calls sts:AssumeRole using the role_arn stored in the secret, then writes the
    new short-lived credentials (access key, secret key, session token, expiry) back
    into the same secret so the next call to get_customer_session can reuse them
    without an extra AssumeRole round-trip.

    Args:
        secrets_client: boto3 secretsmanager client (already scoped to the correct region).
        secret_name: Full Secrets Manager secret name (e.g. ``msp-credentials/acme-prod``).
        creds: Credential dict parsed from the existing secret value.  Must contain
            ``role_arn``; may optionally contain ``external_id``.
        region: AWS region string used to create the STS client.

    Returns:
        Updated credential dict with new ``aws_access_key_id``, ``aws_secret_access_key``,
        ``aws_session_token``, ``expires_at``, and ``generated_at`` values; or ``None``
        if the AssumeRole call fails or ``role_arn`` is absent.
    """
    role_arn = creds.get('role_arn')
    external_id = creds.get('external_id')
    if not role_arn:
        logger.error(f"No role_arn in {secret_name}, cannot refresh")
        return None

    try:
        sts = boto3.client('sts', region_name=region)
        params = {
            'RoleArn': role_arn,
            'RoleSessionName': f"MSP-refresh-{int(time.time())}",
            'DurationSeconds': 3600,
        }
        if external_id:
            params['ExternalId'] = external_id

        response = sts.assume_role(**params)
        new_creds = response['Credentials']

        # Update secret
        creds['aws_access_key_id'] = new_creds['AccessKeyId']
        creds['aws_secret_access_key'] = new_creds['SecretAccessKey']
        creds['aws_session_token'] = new_creds['SessionToken']
        creds['expires_at'] = new_creds['Expiration'].isoformat()
        creds['generated_at'] = datetime.now(timezone.utc).isoformat()

        secrets_client.put_secret_value(
            SecretId=secret_name,
            SecretString=json.dumps(creds)
        )
        logger.info(f"Refreshed credentials for {secret_name}")
        return creds

    except Exception as e:
        logger.error(f"Failed to refresh credentials for {secret_name}: {e}")
        return None
