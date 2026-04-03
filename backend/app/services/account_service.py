# backend/app/services/account_service.py
"""
Account management service wrapping existing Python logic.
Provides REST API interface for customer account lifecycle operations.

Account creation follows a deliberate three-step flow to allow the user to
set up an IAM role in their AWS account between steps:

    Step 1 — Frontend collects account name.
    Step 2 — ``prepare_account()`` generates a stable ``external_id`` and
             ``role_name`` and persists them in Secrets Manager with
             status="preparing".  The caller displays these values so the
             operator can create the corresponding IAM role on the customer
             side.
    Step 3 — ``create_account()`` receives the customer's AWS account ID,
             adds it to the Secrets Manager entry, and immediately attempts
             an STS AssumeRole to verify the IAM role is reachable.  If STS
             succeeds the account status becomes "active"; otherwise it becomes
             "pending" and the operator must fix the IAM trust policy before
             retrying via ``refresh_account()``.

This separation prevents the ``external_id`` from changing between the role-
creation instructions shown to the operator and the actual STS call, which
would cause an ``AccessDenied`` error.
"""

import os
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

from app.core.account_manager import get_cross_account_manager
from app.core.secrets_credential_manager import get_secrets_credential_manager
from app.core.workspace_context import get_workspace_context


class AccountService:
    """
    Service for managing customer accounts via REST API.
    Wraps existing account_manager and secrets_credential_manager logic.
    """
    
    def __init__(self):
        """Initialize account service with existing components."""
        self.account_manager = get_cross_account_manager()
        self.secrets_manager = get_secrets_credential_manager()
        self.workspace = get_workspace_context()
    
    async def prepare_account(self, account_name: str) -> Dict:
        """
        Prepare account by generating external_id and role_name.
        This is called when user transitions from Step 1 to Step 2.
        Stores metadata in Secrets Manager to ensure consistency.
        
        Args:
            account_name: Customer account name (already sanitized from frontend)
            
        Returns:
            Dict with role_name, external_id, and setup instructions
        """
        try:
            import boto3
            import json
            import uuid
            from datetime import datetime
            from app.core.secrets_credential_manager import get_current_msp_principal_arn
            
            secret_name = f"msp-credentials/{account_name}"
            secrets_client = boto3.client('secretsmanager')
            
            # Check if account already exists
            try:
                response = secrets_client.get_secret_value(SecretId=secret_name)
                existing_secret = json.loads(response['SecretString'])
                
                # Return existing configuration
                return {
                    "success": True,
                    "existing": True,
                    "account_name": account_name,
                    "role_name": existing_secret.get('role_name'),
                    "external_id": existing_secret.get('external_id'),
                    "account_id": existing_secret.get('account_id'),
                    "msp_principal_arn": get_current_msp_principal_arn()
                }
            except secrets_client.exceptions.ResourceNotFoundException:
                # Account doesn't exist - generate new identifiers
                pass
            except Exception as e:
                if 'marked for deletion' not in str(e):
                    return {
                        "success": False,
                        "message": f"Error checking existing account: {str(e)}"
                    }
            
            # Generate new identifiers
            external_id = f"msp-{account_name}-{str(uuid.uuid4())[:8]}"
            role_name = f"MSP-{account_name}-Role"
            msp_principal_arn = get_current_msp_principal_arn()
            
            # Store initial metadata (no account_id or credentials yet)
            secret_value = {
                "customer_name": account_name,
                "role_name": role_name,
                "external_id": external_id,
                "status": "preparing",
                "created_at": datetime.now().isoformat(),
                "aws_access_key_id": None,
                "aws_secret_access_key": None,
                "aws_session_token": None,
                "expires_at": None
            }
            
            secrets_client.create_secret(
                Name=secret_name,
                SecretString=json.dumps(secret_value),
                Description=f"MSP cross-account credentials for {account_name}"
            )
            
            logger.info(f"Prepared account {account_name} with external_id: {external_id}")
            
            return {
                "success": True,
                "existing": False,
                "account_name": account_name,
                "role_name": role_name,
                "external_id": external_id,
                "msp_principal_arn": msp_principal_arn
            }
            
        except Exception as e:
            return {
                "success": False,
                "message": f"Account preparation failed: {str(e)}"
            }
    
    async def create_account(self, account_name: str, account_id: str, description: str = None) -> Dict:
        """
        Complete account creation by adding account_id to prepared account.
        This is called at Step 3 after user provides account_id.
        
        IMPORTANT: Account must be prepared first via prepare_account().
        Uses existing external_id and role_name from preparation step.
        
        Args:
            account_name: Customer account name (already sanitized)
            account_id: Customer's AWS account ID
            description: Optional description
            
        Returns:
            Dict with success status and account details
        """
        try:
            import boto3
            import json
            from datetime import datetime
            
            secret_name = f"msp-credentials/{account_name}"
            secrets_client = boto3.client('secretsmanager')
            
            # Read prepared account to get external_id and role_name
            try:
                response = secrets_client.get_secret_value(SecretId=secret_name)
                prepared_secret = json.loads(response['SecretString'])
                
                # Use EXISTING external_id and role_name from preparation
                external_id = prepared_secret.get('external_id')
                role_name = prepared_secret.get('role_name')
                
                if not external_id or not role_name:
                    return {
                        "success": False,
                        "message": "Account not properly prepared - missing external_id or role_name"
                    }
                
                logger.info(f"Using prepared external_id: {external_id}")
                
            except secrets_client.exceptions.ResourceNotFoundException:
                return {
                    "success": False,
                    "message": "Account not prepared - call /accounts/prepare first"
                }
            
            # Update secret with account_id
            prepared_secret['account_id'] = account_id
            prepared_secret['description'] = description or ""
            prepared_secret['status'] = "pending"
            prepared_secret['role_arn'] = f"arn:aws:iam::{account_id}:role/{role_name}"
            prepared_secret['generated_at'] = datetime.now().isoformat()
            
            secrets_client.update_secret(
                SecretId=secret_name,
                SecretString=json.dumps(prepared_secret)
            )
            
            logger.info(f"Updated account {account_name} with account_id: {account_id}")

            # Add to in-memory account manager so subsequent in-process lookups
            # (e.g. workspace context switches) find the account without a
            # Secrets Manager round-trip.
            self.account_manager.add_account(account_name, account_id, description)

            # Immediately attempt STS AssumeRole as a connectivity smoke-test.
            # If the operator has already created the IAM role in the customer
            # account (common in automated flows), this will succeed and the
            # account becomes "active" right away.  If not, the account stays
            # "pending" and the error details are surfaced to the UI so the
            # operator knows exactly what trust-policy fix is required.
            logger.info(f"Attempting to generate STS tokens for {account_name}...")
            token_success, token_error = self.secrets_manager.generate_and_store_tokens(
                account_name,
                account_id,
                role_name,
                external_id
            )
            
            # Determine status and capture error details
            if token_success:
                initial_status = "active"
                needs_refresh = False
                sts_error = None
                logger.info(f"Account {account_name} is now active")
            else:
                initial_status = "pending"
                needs_refresh = True
                sts_error = token_error
                logger.info(f"Account {account_name} is pending - IAM role setup needed")
                logger.info(f"STS Error: {token_error}")
            
            return {
                "success": True,
                "message": f"Account '{account_name}' added successfully",
                "account": {
                    "id": account_name,
                    "name": account_name,
                    "account_id": account_id,
                    "role_name": role_name,
                    "external_id": external_id,
                    "status": initial_status,
                    "created_at": prepared_secret.get('created_at'),
                    "needs_refresh": needs_refresh,
                    "sts_error": sts_error  # Include STS error details for pending accounts
                }
            }
                
        except Exception as e:
            return {
                "success": False,
                "message": f"Account creation failed: {str(e)}"
            }
    
    async def list_accounts(self) -> Dict:
        """
        List all customer accounts from Secrets Manager.
        Returns all accounts even if credentials expired, but SKIPS accounts
        that are pending deletion (marked for deletion).
        
        Returns:
            Dict with account list and status information
        """
        try:
            import boto3
            import json
            
            # Get stored accounts from Secrets Manager (already filters pending deletions)
            stored_account_names = self.secrets_manager.list_stored_accounts()
            
            logger.info(f"Found {len(stored_account_names)} accounts in Secrets Manager: {stored_account_names}")
            
            accounts = []
            for account_name in stored_account_names:
                try:
                    # Try to read secret directly (even if expired)
                    secrets_client = boto3.client('secretsmanager')
                    secret_name = f"msp-credentials/{account_name}"
                    
                    try:
                        response = secrets_client.get_secret_value(SecretId=secret_name)
                        secret_data = json.loads(response['SecretString'])
                        
                        # Determine status based on credential state
                        # Check if account has ever had credentials
                        has_credentials = secret_data.get('aws_access_key_id') is not None
                        
                        if not has_credentials:
                            # Never had credentials - account is pending (waiting for IAM role)
                            status = "pending"
                        else:
                            # Has credentials - test if they're still valid
                            credentials = self.secrets_manager.get_customer_credentials(account_name)
                            if credentials:
                                # Credentials exist and not expired
                                access_success, _ = self.secrets_manager.test_customer_access(account_name)
                                status = "active" if access_success else "expired"
                            else:
                                # Credentials expired or expiring soon
                                status = "expired"
                        
                        # Use customer_name from secret if available, fallback to sanitized path name
                        display_name = secret_data.get('customer_name', account_name)
                        
                        account_data = {
                            "id": account_name,  # Keep sanitized name as ID for API calls
                            "name": display_name,  # Use original customer name for display
                            "account_id": secret_data.get('account_id', 'Unknown'),
                            "role_name": secret_data.get('role_name', f"MSP-{account_name}-Role"),
                            "external_id": secret_data.get('external_id', 'unknown'),
                            "status": status,
                            "created_at": secret_data.get('generated_at'),
                            "needs_refresh": status != "active"
                        }
                        accounts.append(account_data)
                        logger.info(f"Added account: {account_name} (status: {status})")
                        
                    except Exception as secret_error:
                        error_message = str(secret_error)

                        # Secrets Manager schedules deletion with a minimum 7-day recovery
                        # window.  During this window the secret still appears in list_secrets
                        # results (so list_stored_accounts returns it) but get_secret_value
                        # raises an exception containing "marked for deletion".  Silently skip
                        # these to avoid surfacing ghost accounts in the UI.
                        if 'marked for deletion' in error_message.lower():
                            logger.info(f"Skipping {account_name} - secret marked for deletion")
                            continue

                        # For other errors, add with error status
                        logger.warning(f"Could not read secret for {account_name}: {secret_error}")
                        accounts.append({
                            "id": account_name,
                            "name": account_name,
                            "account_id": "Unknown",
                            "role_name": f"MSP-{account_name}-Role",
                            "external_id": "unknown",
                            "status": "error",
                            "created_at": None,
                            "needs_refresh": True
                        })
                        
                except Exception as account_error:
                    error_message = str(account_error)

                    # Outer guard: same deletion-window logic as the inner except above,
                    # catching the case where the deletion error surfaces outside the
                    # get_secret_value call (e.g. during boto3 client initialisation).
                    if 'marked for deletion' in error_message.lower():
                        logger.info(f"Skipping {account_name} - marked for deletion")
                        continue

                    logger.error(f"Error processing account {account_name}: {account_error}")
                    # Still add account with error status for non-deletion errors
                    accounts.append({
                        "id": account_name,
                        "name": account_name,
                        "account_id": "Unknown",
                        "role_name": f"MSP-{account_name}-Role",
                        "external_id": "unknown",
                        "status": "error",
                        "created_at": None,
                        "needs_refresh": True,
                        "error": str(account_error)
                    })
            
            logger.info(f"Returning {len(accounts)} total accounts to frontend")
            
            return {
                "success": True,
                "accounts": accounts,
                "total": len(accounts)
            }
            
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to list accounts: {str(e)}",
                "accounts": []
            }
    
    async def refresh_all_accounts(self) -> Dict:
        """
        Refresh STS tokens for ALL customer accounts.
        Matches Streamlit _refresh_account_status() behavior.
        
        Returns:
            Dict with refresh results
        """
        try:
            import boto3
            import json
            
            # Get all accounts from Secrets Manager
            stored_account_names = self.secrets_manager.list_stored_accounts()
            
            logger.info(f"Refreshing tokens for {len(stored_account_names)} accounts...")
            
            refreshed_count = 0
            failed_count = 0
            results = []
            
            for account_name in stored_account_names:
                try:
                    # Read account config from secret
                    secrets_client = boto3.client('secretsmanager')
                    secret_name = f"msp-credentials/{account_name}"
                    
                    try:
                        response = secrets_client.get_secret_value(SecretId=secret_name)
                        secret_data = json.loads(response['SecretString'])
                        
                        # Create account config for refresh
                        account_config = {
                            'name': account_name,
                            'id': secret_data.get('account_id'),
                            'role': secret_data.get('role_name'),
                            'external_id': secret_data.get('external_id')
                        }
                        
                        # Attempt refresh
                        success, error_msg = self.secrets_manager.refresh_if_needed(account_name, account_config)
                        
                        if success:
                            refreshed_count += 1
                            results.append({
                                "account": account_name,
                                "status": "refreshed",
                                "message": "Tokens refreshed successfully"
                            })
                            logger.info(f"Refreshed tokens for {account_name}")
                        else:
                            failed_count += 1
                            results.append({
                                "account": account_name,
                                "status": "failed",
                                "message": f"Refresh failed: {error_msg}",
                                "error": error_msg
                            })
                            logger.warning(f"Could not refresh tokens for {account_name}: {error_msg}")

                    except Exception as secret_error:
                        failed_count += 1
                        results.append({
                            "account": account_name,
                            "status": "error",
                            "message": f"Secret read error: {str(secret_error)}"
                        })
                        logger.error(f"Secret read error for {account_name}: {secret_error}")

                except Exception as account_error:
                    failed_count += 1
                    results.append({
                        "account": account_name,
                        "status": "error",
                        "message": str(account_error)
                    })
                    logger.error(f"Refresh error for {account_name}: {account_error}")
            
            return {
                "success": True,
                "refreshed": refreshed_count,
                "failed": failed_count,
                "total": len(stored_account_names),
                "results": results,
                "message": f"Refreshed {refreshed_count} account(s), {failed_count} failed"
            }
            
        except Exception as e:
            return {
                "success": False,
                "message": f"Refresh all accounts failed: {str(e)}",
                "refreshed": 0,
                "failed": 0
            }
    
    async def delete_account(self, account_name: str) -> Dict:
        """
        Delete customer account and cleanup credentials.
        Deletes directly from Secrets Manager (matches Streamlit pattern).
        
        Args:
            account_name: Customer account name to delete
            
        Returns:
            Dict with success status
        """
        try:
            logger.info(f"Deleting account: {account_name}")
            
            # Delete credentials from Secrets Manager directly
            # (Don't use account_manager.remove_account - it only works for in-memory accounts)
            success = self.secrets_manager.delete_customer_credentials(account_name)
            
            if success:
                logger.info(f"Deleted credentials from Secrets Manager for {account_name}")

                # Also clear workspace context if this account was selected
                current_account = self.workspace.get_current_account()
                if current_account == account_name:
                    self.workspace.clear_context()
                    logger.info(f"Cleared workspace context for {account_name}")
                
                # Try to remove from account_manager if it exists there
                try:
                    self.account_manager.remove_account(account_name)
                except Exception:  # nosec B110
                    pass  # Ignore errors - account might not be in memory
                
                return {
                    "success": True,
                    "message": f"Successfully deleted {account_name} account and credentials"
                }
            else:
                return {
                    "success": False,
                    "message": f"Failed to delete credentials for {account_name}"
                }
            
        except Exception as e:
            logger.error(f"Delete account error: {e}")
            return {
                "success": False,
                "message": f"Account deletion failed: {str(e)}"
            }
    
    async def refresh_account(self, account_name: str) -> Dict:
        """
        Refresh STS tokens for customer account.
        
        Args:
            account_name: Customer account name to refresh
            
        Returns:
            Dict with refresh status
        """
        try:
            import boto3
            import json
            
            # Read secret directly (don't use get_customer_credentials which returns None for expired)
            secrets_client = boto3.client('secretsmanager')
            secret_name = f"msp-credentials/{account_name}"
            
            try:
                response = secrets_client.get_secret_value(SecretId=secret_name)
                secret_data = json.loads(response['SecretString'])
            except Exception as e:
                return {
                    "success": False,
                    "message": f"Could not read account secret: {str(e)}"
                }
            
            # Extract account config (works even if credentials expired)
            account_config = {
                'name': account_name,
                'id': secret_data.get('account_id'),
                'role': secret_data.get('role_name'),
                'external_id': secret_data.get('external_id')
            }
            
            # Force regenerate tokens (don't check expiry - user explicitly requested refresh)
            success, error_msg = self.secrets_manager.generate_and_store_tokens(
                account_name,
                account_config['id'],
                account_config['role'],
                account_config['external_id']
            )
            
            if success:
                # Test access after refresh
                access_success, access_message = self.secrets_manager.test_customer_access(account_name)
                
                return {
                    "success": True,
                    "message": "Account tokens refreshed successfully",
                    "access_test": {
                        "success": access_success,
                        "message": access_message
                    }
                }
            else:
                return {
                    "success": False,
                    "message": f"Failed to refresh account tokens: {error_msg}",
                    "error": error_msg
                }
                
        except Exception as e:
            return {
                "success": False,
                "message": f"Token refresh failed: {str(e)}"
            }
    
    async def test_account_access(self, account_name: str) -> Dict:
        """
        Test access to customer account.
        
        Args:
            account_name: Customer account name to test
            
        Returns:
            Dict with access test results
        """
        try:
            success, message = self.secrets_manager.test_customer_access(account_name)
            
            return {
                "success": success,
                "message": message,
                "tested_at": datetime.now().isoformat()
            }
            
        except Exception as e:
            return {
                "success": False,
                "message": f"Access test failed: {str(e)}"
            }
    
    async def get_account_status(self, account_name: str) -> Dict:
        """
        Get comprehensive account status information.
        
        Args:
            account_name: Customer account name
            
        Returns:
            Dict with detailed account status
        """
        try:
            # Use existing account manager status logic
            status_info = self.account_manager.get_account_status(account_name)
            
            return {
                "success": True,
                "status": status_info
            }
            
        except Exception as e:
            return {
                "success": False,
                "message": f"Status check failed: {str(e)}"
            }
    
    async def switch_account_context(self, account_name: Optional[str]) -> Dict:
        """
        Switch workspace context to specified account.
        
        Args:
            account_name: Account name to switch to (None for MSP)
            
        Returns:
            Dict with switch operation result
        """
        try:
            if account_name is None or account_name == "default":
                # Switch to MSP account
                self.workspace.clear_context()
                context_display = self.workspace.get_account_context_display()
                
                return {
                    "success": True,
                    "message": "Switched to MSP account",
                    "account_context": context_display
                }
            else:
                # Switch to customer account
                success = self.workspace.set_current_account(account_name)
                
                if success:
                    context_display = self.workspace.get_account_context_display()
                    
                    # Test current access
                    access_success, access_message = self.workspace.test_current_access()
                    
                    return {
                        "success": True,
                        "message": f"Switched to {account_name}",
                        "account_context": context_display,
                        "access_test": {
                            "success": access_success,
                            "message": access_message
                        }
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Failed to switch to {account_name}"
                    }
                    
        except Exception as e:
            return {
                "success": False,
                "message": f"Account switch failed: {str(e)}"
            }


# Singleton instance
_account_service = None

def get_account_service() -> AccountService:
    """Get singleton AccountService instance."""
    global _account_service
    if _account_service is None:
        _account_service = AccountService()
    return _account_service
