"""
Credential lifecycle manager for MSP cross-account STS tokens.

Architecture context:
  This module is the single source of truth for customer AWS credentials.
  The lifecycle it manages is:

    generate  ──  assume_role (STS) to obtain temporary credentials
    store     ──  write JSON blob to Secrets Manager under msp-credentials/<account>
    retrieve  ──  read + validity-check credentials; return None if missing/expiring
    refresh   ──  called by workspace_context.py when credentials are about to expire
    delete    ──  remove the Secrets Manager secret on account removal

  All credential operations use the MSP identity (ECS task role in production,
  AWS_PROFILE locally). Customer boto3 sessions are built from the retrieved STS
  tokens and never held here long-term.

  Singleton access: use get_secrets_credential_manager() rather than constructing
  SecretsCredentialManager directly, to avoid redundant STS/Secrets Manager clients.
"""
import boto3
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Any
from botocore.exceptions import ClientError
import uuid

logger = logging.getLogger(__name__)


class SecretsCredentialManager:
    """
    Manages STS credentials for customer accounts using AWS Secrets Manager.
    Handles token generation, storage, refresh, and retrieval.
    """
    
    def __init__(self, aws_profile: str = None):
        """
        Initialize the Secrets Manager client
        
        Args:
            aws_profile: AWS profile to use (defaults to current session)
        """
        try:
            if aws_profile:
                session = boto3.Session(profile_name=aws_profile)
            else:
                session = boto3.Session()
                
            self._secrets_client = session.client('secretsmanager')
            self._sts_client = session.client('sts')
            
            # Get current MSP account ID dynamically
            self.msp_account_id = self._get_current_msp_account_id()
            logger.info(f"MSP Account ID: {self.msp_account_id}")
            
        except Exception as e:
            raise RuntimeError(f"Failed to initialize SecretsCredentialManager: {e}")
    
    def _get_current_msp_account_id(self) -> str:
        """Get the current MSP account ID dynamically.

        Returns:
            12-digit AWS account ID string for the executing identity.

        Raises:
            RuntimeError: If the STS call fails (e.g., no network, invalid credentials).
        """
        try:
            identity = self._sts_client.get_caller_identity()
            return identity['Account']
        except Exception as e:
            raise RuntimeError(f"Failed to get MSP account ID: {e}")
    
    def get_current_msp_principal_arn(self) -> str:
        """Get the current MSP user/role ARN dynamically.

        Returns:
            Full ARN string for the current IAM user or assumed role.

        Raises:
            RuntimeError: If the STS call fails.
        """
        try:
            identity = self._sts_client.get_caller_identity()
            return identity['Arn']
        except Exception as e:
            raise RuntimeError(f"Failed to get MSP principal ARN: {e}")
    
    def generate_and_store_tokens(self, account_name: str, customer_account_id: str, 
                                role_name: str = None, external_id: str = None) -> tuple[bool, str]:
        """
        Generate STS tokens for customer account and store in Secrets Manager
        
        Args:
            account_name: Customer account name
            customer_account_id: Customer AWS account ID
            role_name: Cross-account role name (defaults to MSP-{account_name}-Role)
            external_id: External ID for role assumption (generated if not provided)
            
        Returns:
            Tuple of (success: bool, error_message: str)
            - (True, "") if successful
            - (False, error_message) if failed with detailed error
        """
        try:
            # Generate default values if not provided
            if not role_name:
                clean_name = account_name.replace(' ', '').replace('-', '').replace('_', '')
                role_name = f"MSP-{clean_name}-Role"
            
            if not external_id:
                external_id = f"msp-{account_name.lower().replace(' ', '-')}-{str(uuid.uuid4())[:8]}"
            
            # Construct role ARN
            role_arn = f"arn:aws:iam::{customer_account_id}:role/{role_name}"
            session_name = f"MSP-{account_name}-{int(time.time())}"
            
            logger.info(f"Assuming role: {role_arn}")
            
            # Assume role to get STS credentials
            response = self._sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                ExternalId=external_id,
                DurationSeconds=3600  # 1 hour
            )
            
            credentials = response['Credentials']
            
            # Prepare secret value
            secret_value = {
                "aws_access_key_id": credentials['AccessKeyId'],
                "aws_secret_access_key": credentials['SecretAccessKey'], 
                "aws_session_token": credentials['SessionToken'],
                "expires_at": credentials['Expiration'].isoformat(),
                "account_id": customer_account_id,
                "customer_name": account_name,
                "role_arn": role_arn,
                "role_name": role_name,
                "external_id": external_id,
                "generated_at": datetime.now().isoformat()
            }
            
            # Store in Secrets Manager (account_name already sanitized from frontend)
            secret_name = f"msp-credentials/{account_name}"
            
            try:
                # Try to update existing secret
                self._secrets_client.update_secret(
                    SecretId=secret_name,
                    SecretString=json.dumps(secret_value)
                )
                logger.info(f"Updated STS tokens for {account_name}")
                
            except ClientError as e:
                if e.response['Error']['Code'] == 'ResourceNotFoundException':
                    # Create new secret if it doesn't exist
                    self._secrets_client.create_secret(
                        Name=secret_name,
                        SecretString=json.dumps(secret_value),
                        Description=f"MSP cross-account STS tokens for {account_name}"
                    )
                    logger.info(f"Created STS tokens for {account_name}")
                else:
                    raise
            
            return (True, "")
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_msg = e.response.get('Error', {}).get('Message', str(e))
            full_error = f"{error_code}: {error_msg}"
            logger.error(f"Failed to generate STS tokens for {account_name}: {full_error}")
            return (False, full_error)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to generate STS tokens for {account_name}: {error_msg}")
            return (False, error_msg)
    
    def get_customer_credentials(self, account_name: str) -> Optional[Dict[str, str]]:
        """
        Retrieve customer credentials from Secrets Manager
        
        Args:
            account_name: Customer account name (already sanitized from frontend)
            
        Returns:
            Dictionary with AWS credentials or None if not found/expired
        """
        try:
            secret_name = f"msp-credentials/{account_name}"
            
            response = self._secrets_client.get_secret_value(SecretId=secret_name)
            secret_data = json.loads(response['SecretString'])
            
            # Check if credentials exist (expires_at will be None for newly created accounts without STS tokens)
            if not secret_data.get('expires_at') or not secret_data.get('aws_access_key_id'):
                logger.info(f"No stored credentials found for {account_name}")
                return None

            # Check if credentials are still valid
            expires_at = datetime.fromisoformat(secret_data['expires_at'].replace('Z', '+00:00'))

            # Refresh if expiring within 10 minutes
            if expires_at <= datetime.now(timezone.utc) + timedelta(minutes=10):
                logger.info(f"Credentials for {account_name} expiring soon, will need refresh")
                return None

            logger.info(f"Retrieved valid credentials for {account_name}")
            return secret_data
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.info(f"No stored credentials found for {account_name}")
            else:
                logger.error(f"Error retrieving credentials for {account_name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error retrieving credentials for {account_name}: {e}")
            return None
    
    def refresh_if_needed(self, account_name: str, account_config: Dict[str, Any]) -> tuple[bool, str]:
        """
        Refresh credentials if they are expired or expiring soon
        
        Args:
            account_name: Customer account name
            account_config: Account configuration with ID, role, external_id
            
        Returns:
            Tuple of (success: bool, error_message: str)
        """
        try:
            credentials = self.get_customer_credentials(account_name)
            
            if credentials is None:
                # Need to generate new credentials
                return self.generate_and_store_tokens(
                    account_name,
                    account_config.get('id', account_config.get('account_id')),
                    account_config.get('role'),
                    account_config.get('external_id')
                )
            
            # Credentials are valid
            return (True, "")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error refreshing credentials for {account_name}: {error_msg}")
            return (False, error_msg)
    
    def delete_customer_credentials(self, account_name: str) -> bool:
        """
        Delete stored credentials for a customer account
        
        Args:
            account_name: Customer account name (already sanitized from frontend)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            secret_name = f"msp-credentials/{account_name}"
            
            self._secrets_client.delete_secret(
                SecretId=secret_name,
                ForceDeleteWithoutRecovery=True
            )
            
            logger.info(f"Deleted credentials for {account_name}")
            return True
            
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.info(f"No credentials found to delete for {account_name}")
                return True  # Already deleted
            else:
                logger.error(f"Error deleting credentials for {account_name}: {e}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error deleting credentials for {account_name}: {e}")
            return False
    
    def test_customer_access(self, account_name: str) -> tuple[bool, str]:
        """
        Test if customer credentials are working
        
        Args:
            account_name: Customer account name
            
        Returns:
            Tuple of (success, message)
        """
        try:
            credentials = self.get_customer_credentials(account_name)
            
            if not credentials:
                return False, "No valid credentials found"
            
            # Create session with customer credentials
            session = boto3.Session(
                aws_access_key_id=credentials['aws_access_key_id'],
                aws_secret_access_key=credentials['aws_secret_access_key'],
                aws_session_token=credentials['aws_session_token']
            )
            
            # Test access with STS get_caller_identity
            sts_client = session.client('sts')
            identity = sts_client.get_caller_identity()
            
            customer_account_id = identity['Account']
            
            return True, f"Access confirmed for account {customer_account_id}"
            
        except Exception as e:
            return False, f"Access test failed: {str(e)}"
    
    def list_stored_accounts(self) -> list[str]:
        """
        List all customer accounts with stored credentials.
        Excludes secrets that are marked for deletion or pending deletion.
        Validates each secret's accessibility before including in list.
        
        Returns:
            List of account names (only accessible secrets)
        """
        try:
            paginator = self._secrets_client.get_paginator('list_secrets')
            
            account_names = []
            skipped_accounts = []
            
            for page in paginator.paginate():
                for secret in page['SecretList']:
                    # Skip secrets that are marked for deletion (in recovery window)
                    if 'DeletedDate' in secret:
                        continue
                    
                    secret_name = secret['Name']
                    if secret_name.startswith('msp-credentials/'):
                        account_name = secret_name.replace('msp-credentials/', '')
                        
                        # Validate secret is accessible (not pending deletion)
                        try:
                            self._secrets_client.get_secret_value(SecretId=secret_name)
                            account_names.append(account_name)
                        except ClientError as e:
                            error_code = e.response.get('Error', {}).get('Code', '')
                            error_message = str(e).lower()
                            
                            # Skip secrets that are pending deletion (ForceDeleteWithoutRecovery)
                            if 'marked for deletion' in error_message or error_code == 'InvalidRequestException':
                                logger.info(f"[list_stored_accounts] Filtering out {account_name} - marked for deletion")
                                skipped_accounts.append(account_name)
                                continue
                            elif error_code == 'ResourceNotFoundException':
                                logger.info(f"[list_stored_accounts] Filtering out {account_name} - not found")
                                skipped_accounts.append(account_name)
                                continue
                            else:
                                # Other errors - still include the account but log warning
                                logger.warning(f"[list_stored_accounts] Warning for {account_name}: {e}")
                                account_names.append(account_name)
                        except Exception as e:
                            error_str = str(e).lower()
                            # Check for deletion errors in generic exceptions too
                            if 'marked for deletion' in error_str:
                                logger.info(f"[list_stored_accounts] Filtering out {account_name} - marked for deletion (generic)")
                                skipped_accounts.append(account_name)
                                continue
                            # Unexpected error - include account but log warning
                            logger.warning(f"[list_stored_accounts] Unexpected error for {account_name}: {e}")
                            account_names.append(account_name)
            
            if skipped_accounts:
                logger.info(f"Filtered out {len(skipped_accounts)} deleted accounts: {skipped_accounts}")

            return account_names

        except Exception as e:
            logger.error(f"Error listing stored accounts: {e}")
            return []


# Cached singleton — avoids creating new boto3 clients on every request.
# aws_profile is typically None in ECS (uses task role), so one instance suffices.
_secrets_credential_manager_instance: "SecretsCredentialManager | None" = None


def get_secrets_credential_manager(aws_profile: str = None) -> "SecretsCredentialManager":
    """Return a cached SecretsCredentialManager singleton."""
    global _secrets_credential_manager_instance
    if _secrets_credential_manager_instance is None:
        _secrets_credential_manager_instance = SecretsCredentialManager(aws_profile)
    return _secrets_credential_manager_instance


# Utility functions for easy access
def get_current_msp_account_id() -> str:
    """Get the current MSP account ID dynamically.

    Returns:
        12-digit AWS account ID string for the executing identity.

    Raises:
        RuntimeError: If the STS call fails (no credentials, network error, etc.).
    """
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()
        return identity['Account']
    except Exception as e:
        raise RuntimeError(f"Failed to get MSP account ID: {e}")

def get_current_msp_principal_arn() -> str:
    """
    Get the current MSP user/role ARN dynamically.
    Converts session ARNs to stable IAM role ARNs for trust policies.

    Returns:
        Stable IAM ARN suitable for use in trust policy Principal fields.
        For assumed-role sessions this is the role ARN, not the session ARN.

    Raises:
        RuntimeError: If the STS call fails.
    """
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()
        arn = identity['Arn']

        # Convert session ARN to IAM role ARN for trust policies.
        # STS returns a session-scoped ARN like:
        #   arn:aws:sts::ACCOUNT:assumed-role/ROLE/SESSION
        # Trust policies require the permanent IAM role ARN:
        #   arn:aws:iam::ACCOUNT:role/ROLE
        # Splitting on ':' yields parts[4]=ACCOUNT, parts[5]="assumed-role/ROLE/SESSION".
        if ':assumed-role/' in arn:
            parts = arn.split(':')
            account = parts[4]
            role_and_session = parts[5].split('/')
            # Index 0 = "assumed-role", index 1 = ROLE name, index 2 = session name
            role_name = role_and_session[1]
            stable_arn = f"arn:aws:iam::{account}:role/{role_name}"
            logger.info(f"Converted session ARN to stable IAM role ARN: {stable_arn}")
            return stable_arn

        # Already an IAM user/role ARN - return as-is
        return arn
    except Exception as e:
        raise RuntimeError(f"Failed to get MSP principal ARN: {e}")

def get_current_msp_principal_info() -> dict:
    """Get complete MSP principal information.

    Returns:
        Dict with keys: account_id, user_id, arn, principal_type ('user' or 'role').

    Raises:
        RuntimeError: If the STS call fails.
    """
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()

        return {
            'account_id': identity['Account'],
            'user_id': identity['UserId'],
            'arn': identity['Arn'],
            # Distinguish IAM users from roles/task roles for trust policy generation
            'principal_type': 'user' if ':user/' in identity['Arn'] else 'role'
        }
    except Exception as e:
        raise RuntimeError(f"Failed to get MSP principal info: {e}")


def get_selected_customer_account_id(account_name: str) -> Optional[str]:
    """Get the account ID for a selected customer account"""
    try:
        secrets_manager = get_secrets_credential_manager()
        credentials = secrets_manager.get_customer_credentials(account_name)
        
        if credentials:
            return credentials.get('account_id')
        
        return None
        
    except Exception as e:
        logger.error(f"Error getting customer account ID for {account_name}: {e}")
        return None
