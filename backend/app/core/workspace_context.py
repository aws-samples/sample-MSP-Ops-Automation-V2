"""
Per-request credential isolation via Python ContextVar.

Architecture context:
  In an async FastAPI/ECS deployment multiple coroutines can be in-flight
  simultaneously within the same OS thread. A module-level global for the
  "current account" would cause one request to see another request's credentials
  (a serious security bug). This module solves that with two ContextVars:

    _ctx_account      — the customer account name for the current async task
    _ctx_credentials  — the STS credential dict for the current async task

  Each ContextVar stores an independent value per asyncio Task (similar to
  thread-local storage but for coroutines). Switching accounts in one task
  is invisible to all other concurrent tasks.

  WorkspaceContext is a single process-wide instance (get_workspace_context()).
  It owns two categories of state:
    - Per-request state  → stored in ContextVars (zero shared mutable state)
    - Shared cache state → _last_credentials_metadata, lazy manager references
      (protected by _lock for thread safety)

  Credential refresh flow:
    set_current_account()
      → fast-path: if same account + not expired, skip Secrets Manager call
      → slow-path: fetch from Secrets Manager outside lock (avoids I/O under lock)
      → _refresh_mcp_caches_if_needed() under lock: clear MCP cache only on change
      → set ContextVars (no lock needed — ContextVars are task-local)
"""
import boto3
import logging
import threading
from typing import Dict, Optional, Any, Tuple
from contextvars import ContextVar
from app.core.secrets_credential_manager import get_secrets_credential_manager, get_current_msp_account_id
from app.core.account_manager import get_cross_account_manager
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Per-request context variables — safe for concurrent async tasks in ECS
_ctx_account: ContextVar[Optional[str]] = ContextVar('ws_account', default=None)
_ctx_credentials: ContextVar[Optional[Dict]] = ContextVar('ws_credentials', default=None)

# Load environment variables
load_dotenv()
AWS_PROFILE = os.getenv('AWS_PROFILE') or None

# Import for accessing cached managers (avoid circular import at module level)


class CredentialMetadata:
    """
    Lightweight metadata for cached credential comparison.
    
    Purpose: Enable fast credential change detection without storing
    full credentials in memory (security best practice).
    """
    
    def __init__(self, account_name: str, credentials: Dict[str, str]):
        """
        Initialize credential metadata from credentials dict.
        
        Args:
            account_name: Customer account identifier
            credentials: Full credentials dict with aws_access_key_id, etc.
        """
        self.account_name = account_name
        self.access_key_prefix = credentials['aws_access_key_id'][:10]
        self.created_at = datetime.now()
        # STS tokens expire after 60 minutes
        self.expires_at = self.created_at + timedelta(minutes=60)
    
    def is_expired(self) -> bool:
        """Check if credentials past 60-min STS lifetime"""
        return datetime.now() >= self.expires_at
    
    def matches_credentials(self, credentials: Dict[str, str]) -> bool:
        """Check if provided credentials match this metadata"""
        return credentials['aws_access_key_id'][:10] == self.access_key_prefix


class WorkspaceContext:
    """
    Manages workspace context for account-aware operations.
    Provides credential resolution and environment variable preparation for MCP clients.
    """
    
    def __init__(self):
        """Initialize workspace context manager"""
        # _current_account and _current_credentials are stored in ContextVars
        # so each concurrent async task gets its own copy (fixes ECS race condition)
        self._secrets_manager = None
        self._account_manager = None
        self._last_credentials_metadata: Optional[CredentialMetadata] = None
        # Lock protects shared mutable state (_last_credentials_metadata,
        # _secrets_manager, _account_manager) accessed by concurrent requests
        self._lock = threading.Lock()
    
    def set_current_account(self, account_name: str) -> bool:
        """
        Set the current working account
        
        Args:
            account_name: Customer account name or None for MSP account
            
        Returns:
            True if account was set successfully
        """
        try:
            logger.debug(f"set_current_account called with: {account_name}")

            if account_name is None or account_name == "Default (Current MSP)":
                _ctx_account.set(None)
                _ctx_credentials.set(None)
                logger.info("Workspace switched to MSP account")
                return True

            with self._lock:
                # Lazy initialisation: create manager singletons on first use so
                # the module can be imported without triggering boto3 client creation.
                if not self._secrets_manager:
                    self._secrets_manager = get_secrets_credential_manager(AWS_PROFILE)
                if not self._account_manager:
                    self._account_manager = get_cross_account_manager()

                # Fast path: skip Secrets Manager API call if same account and credentials not yet expired.
                # Conditions checked under lock (metadata is shared mutable state):
                #   1. metadata exists (we've seen this account before)
                #   2. same account name (no account switch)
                #   3. this task's ContextVar already has credentials (task isn't fresh)
                #   4. credentials haven't reached the 60-min STS expiry
                if (self._last_credentials_metadata is not None
                        and self._last_credentials_metadata.account_name == account_name
                        and _ctx_credentials.get() is not None):
                    now = datetime.now()
                    if now < self._last_credentials_metadata.expires_at:
                        mins_left = (self._last_credentials_metadata.expires_at - now).total_seconds() / 60
                        logger.debug(f"Credentials cache hit for {account_name} ({mins_left:.1f} min remaining)")
                        return True
                # Capture the manager reference while still holding the lock so it
                # cannot be replaced by another thread between the lock release and
                # the network call below.
                secrets_mgr = self._secrets_manager

            # Secrets Manager is a network call — run outside the lock so other
            # concurrent requests are not blocked waiting for I/O
            logger.debug(f"Fetching credentials from Secrets Manager for {account_name}")
            credentials = secrets_mgr.get_customer_credentials(account_name)
            if not credentials:
                logger.warning(f"No valid credentials found for {account_name}")
                return False

            # Re-acquire lock to update shared metadata and conditionally clear caches.
            # ContextVar assignments below are intentionally outside the lock:
            # they are task-local writes and need no synchronisation.
            with self._lock:
                self._refresh_mcp_caches_if_needed(account_name, credentials)

            _ctx_account.set(account_name)
            _ctx_credentials.set(credentials)
            logger.info(f"Workspace switched to customer account: {account_name} ({credentials.get('account_id')})")
            return True

        except Exception as e:
            logger.error(f"Error setting current account to {account_name}: {e}", exc_info=True)
            return False
    
    def get_current_account(self) -> Optional[str]:
        """
        Get the current working account name

        Returns:
            Current account name or None if using MSP account
        """
        return _ctx_account.get()
    
    def get_current_credentials_env(self) -> Optional[Dict[str, str]]:
        """
        Get current credentials as environment variables for MCP clients

        Returns:
            Dictionary of AWS environment variables or None for MSP account
        """
        creds = _ctx_credentials.get()
        if not creds:
            # Using MSP account - no custom environment needed
            return None

        return {
            "AWS_ACCESS_KEY_ID": creds['aws_access_key_id'],
            "AWS_SECRET_ACCESS_KEY": creds['aws_secret_access_key'],
            "AWS_SESSION_TOKEN": creds['aws_session_token']
        }
    
    def get_current_session(self) -> Optional[boto3.Session]:
        """
        Get current boto3 session

        Returns:
            boto3.Session with current credentials or None for MSP account
        """
        creds = _ctx_credentials.get()
        if not creds:
            return None

        return boto3.Session(
            aws_access_key_id=creds['aws_access_key_id'],
            aws_secret_access_key=creds['aws_secret_access_key'],
            aws_session_token=creds['aws_session_token']
        )
    
    def get_current_account_id(self) -> Optional[str]:
        """
        Get current account ID

        Returns:
            AWS account ID or None if using MSP account
        """
        creds = _ctx_credentials.get()
        if creds:
            return creds.get('account_id')
        return None

    def is_customer_account(self) -> bool:
        """
        Check if currently using a customer account

        Returns:
            True if using customer credentials, False if MSP
        """
        return _ctx_account.get() is not None

    def get_account_context_display(self) -> str:
        """
        Get display string for current account context

        Returns:
            Human-readable account context string
        """
        if _ctx_account.get() and _ctx_credentials.get():
            account_id = _ctx_credentials.get().get('account_id', 'Unknown')
            return f"{_ctx_account.get()} ({account_id})"
        else:
            try:
                # Get MSP account ID dynamically
                from app.core.secrets_credential_manager import get_current_msp_account_id
                msp_account_id = get_current_msp_account_id()
                return f"Default MSP ({msp_account_id})"
            except:
                return "Default MSP"
    
    def test_current_access(self) -> tuple[bool, str]:
        """
        Test access with current credentials
        
        Returns:
            Tuple of (success, message)
        """
        try:
            # Check for expired credentials first
            is_expired, expiry_msg = self._handle_credential_expiration()
            if is_expired:
                return False, expiry_msg
            
            if _ctx_account.get() and self._secrets_manager:
                # Test customer account access
                return self._secrets_manager.test_customer_access(_ctx_account.get())
            else:
                # Test MSP account access
                sts = boto3.client('sts')
                identity = sts.get_caller_identity()
                account_id = identity['Account']
                return True, f"MSP account access confirmed: {account_id}"
                
        except Exception as e:
            return False, f"Access test failed: {str(e)}"
    
    def _handle_credential_expiration(self) -> Tuple[bool, str]:
        """
        Check if current credentials have expired and provide user feedback.
        
        Returns:
            Tuple of (is_expired, message_for_user)
        """
        # No credentials metadata means we haven't set credentials yet
        if self._last_credentials_metadata is None:
            return False, ""
        
        # Check if credentials expired
        if self._last_credentials_metadata.is_expired():
            account = self._last_credentials_metadata.account_name
            mins_since_created = (datetime.now() - self._last_credentials_metadata.created_at).total_seconds() / 60
            
            message = f"""
⏰ **Credentials Expired for {account}**

Your AWS temporary credentials have expired after {mins_since_created:.0f} minutes.
STS temporary credentials have a 60-minute lifetime.

**To continue working:**
1. Click the "Refresh" button next to the account in the account selector
2. Or switch to a different account and back to refresh credentials
3. Your next query will automatically use the refreshed credentials

The system will clear cached MCP clients and reconnect with new credentials.
"""
            return True, message.strip()
        
        return False, ""
    
    def clear_context(self):
        """Clear current workspace context"""
        _ctx_account.set(None)
        _ctx_credentials.set(None)
        with self._lock:
            self._last_credentials_metadata = None
        self._clear_mcp_caches()
        logger.info("Cleared workspace context - using MSP account")
    
    def _credentials_changed(self, account_name: str, new_credentials: Dict) -> bool:
        """
        Detect if credentials have actually changed for an account.
        
        Checks three scenarios:
        1. Switching to a different account (different account name)
        2. Same account but different credentials (access key changed)
        3. Same account/credentials but expired (>60 mins old)
        
        Args:
            account_name: Account being switched to
            new_credentials: New credential set from Secrets Manager
            
        Returns:
            True if credentials changed or expired, False if can reuse cache
        """
        if self._last_credentials_metadata is None:
            logger.debug(f"First time credentials for {account_name}")
            return True

        if self._last_credentials_metadata.account_name != account_name:
            logger.debug(f"Account changed: {self._last_credentials_metadata.account_name} -> {account_name}")
            return True

        if self._last_credentials_metadata.is_expired():
            mins = (datetime.now() - self._last_credentials_metadata.created_at).total_seconds() / 60
            logger.info(f"Credentials expired for {account_name} (age: {mins:.1f} minutes)")
            return True

        if not self._last_credentials_metadata.matches_credentials(new_credentials):
            logger.info(f"Access key changed for {account_name}")
            return True

        mins = (datetime.now() - self._last_credentials_metadata.created_at).total_seconds() / 60
        logger.debug(f"Credentials unchanged for {account_name} (age: {mins:.1f} min, {60 - mins:.1f} min until expiry)")
        return False
    
    def _refresh_mcp_caches_if_needed(self, account_name: str, credentials: Dict):
        """
        Selectively clear MCP caches only when credentials change.
        
        This is the SMART cache invalidation logic that:
        1. Checks if credentials actually changed
        2. Only clears cache if needed
        3. Updates metadata for next comparison
        4. Logs cache hit/miss for monitoring
        
        Args:
            account_name: Account being switched to  
            credentials: Current credentials for the account
        """
        # Check if credentials changed
        if self._credentials_changed(account_name, credentials):
            logger.info(f"Credentials changed - clearing MCP cache for {account_name}")
            from app.core.shared_mcp_client import SharedMCPClient
            SharedMCPClient.clear_customer_cache(account_name)
            self._last_credentials_metadata = CredentialMetadata(account_name, credentials)
            logger.debug(f"Updated credential metadata for {account_name}")
        else:
            logger.debug(f"Reusing cached MCP clients for {account_name}")
    
    def _clear_mcp_caches(self):
        """
        Clear MCP client caches to force fresh connections.
        
        DEPRECATED: This method clears ALL caches unconditionally.
        New code should use _refresh_mcp_caches_if_needed() which only clears
        when credentials change, providing much better performance.
        
        This method is kept for backward compatibility (e.g., clear_context()).
        """
        try:
            # Import here to avoid circular imports
            from app.core.shared_mcp_client import SharedMCPClient
            
            # Clear all customer MCP client caches
            SharedMCPClient.clear_customer_cache()
            logger.debug("Cleared all MCP client caches")

        except Exception as e:
            logger.warning(f"Could not clear MCP caches: {e}")


# Global workspace context instance
_workspace_context = WorkspaceContext()

def get_workspace_context() -> WorkspaceContext:
    """
    Get the global workspace context instance
    
    Returns:
        WorkspaceContext instance
    """
    return _workspace_context

def set_current_workspace_account(account_name: str) -> bool:
    """
    Convenience function to set current workspace account
    
    Args:
        account_name: Customer account name or None for MSP
        
    Returns:
        True if successful
    """
    return get_workspace_context().set_current_account(account_name)

def get_current_workspace_account() -> Optional[str]:
    """
    Convenience function to get current workspace account
    
    Returns:
        Current account name or None for MSP
    """
    return get_workspace_context().get_current_account()
