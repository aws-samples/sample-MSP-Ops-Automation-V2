// frontend/src/components/NavigationPanel.tsx
/**
 * Left-sidebar navigation panel for the MSP assistant.
 *
 * Responsibilities:
 * - Account selector: loads the account list on mount, exposes Add / Delete / Refresh
 *   controls, and calls `apiClient.switchAccount` when the selection changes so the
 *   backend targets the correct AWS account for subsequent requests.
 * - Workflow mode selector: delegates to `WorkflowSelector` which toggles flags in
 *   `workflowStore`.
 * - Approval cards: hosts `ApprovalCards` inline in the sidebar so approval gates appear
 *   alongside the chat without requiring a separate panel.
 * - Sample questions: quick-fill shortcuts that populate the chat input.
 * - Health dashboard: live AWS Health event summary.
 * - User info and sign-out.
 *
 * The Refresh button follows a two-step flow: it first calls `refreshAllAccounts` to
 * re-assume STS roles for every customer account (updating secrets in Secrets Manager),
 * then calls `fetchAccounts` to reload the updated status into the selector.  Any
 * per-account failures are surfaced as an inline warning alert.
 */

import React, { useState, useEffect } from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Select, { SelectProps } from '@cloudscape-design/components/select';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { useAuth } from '../hooks/useAuth';
import { useAccountStore } from '../store/accountStore';
import { useChatStore } from '../store/chatStore';
import { WorkflowSelector } from './WorkflowSelector';
import { SampleQuestions } from './SampleQuestions';
import { AddAccountModal } from './AddAccountModal';
import { DeleteAccountModal } from './DeleteAccountModal';
import { HealthDashboard } from './HealthDashboard';
import { ApprovalCards } from './ApprovalCards';
import { apiClient } from '../services/api/apiClient';
import type { Account } from '../types';

export function NavigationPanel() {
  const { signOut, user } = useAuth();
  const { selectedAccount, setSelectedAccount } = useAccountStore();
  const { setInputValue, clearMessages } = useChatStore();
  const [showAddAccountModal, setShowAddAccountModal] = useState(false);
  const [showDeleteAccountModal, setShowDeleteAccountModal] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [isLoadingAccounts, setIsLoadingAccounts] = useState(false);
  const [refreshError, setRefreshError] = useState('');

  // Fetch accounts from backend
  const fetchAccounts = async () => {
    setIsLoadingAccounts(true);
    try {
      const accountList = await apiClient.getAccounts();
      setAccounts(accountList);
      
      // Set default selected account if none selected
      if (!selectedAccount && accountList.length > 0) {
        const defaultAccount = accountList.find(acc => acc.id === 'default') || accountList[0];
        setSelectedAccount(defaultAccount);
      }
    } catch (error) {
      console.error('Failed to fetch accounts:', error);
      // Fallback to default MSP account
      setAccounts([
        {
          id: 'default',
          name: 'Default (Current MSP)',
          type: 'msp',
          status: 'active'
        }
      ]);
    } finally {
      setIsLoadingAccounts(false);
    }
  };

  // Load accounts on mount
  useEffect(() => {
    fetchAccounts();
  }, []);

  // Status icon helper (must be defined before use)
  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'active': return 'Active';
      case 'expired': return 'Expired';
      case 'error': return 'Error';
      default: return 'Unknown';
    }
  };

  // Convert accounts to select options
  const accountOptions: SelectProps.Options = (accounts ?? []).map(account => ({
    label: account.type === 'customer' 
      ? `${getStatusIcon(account.status)} ${account.name} (${account.account_id})`
      : account.name,
    value: account.id,
    iconName: account.type === 'msp' ? 'settings' : 'user-profile'
  }));

  const currentOption = accountOptions.find(opt => opt.value === selectedAccount?.id) || accountOptions[0] || null;

  const handleAccountChange = async (option: SelectProps.Option | null) => {
    if (!option) return;
    const account = (accounts ?? []).find(acc => acc.id === option.value);
    if (account) {
      setSelectedAccount(account);
    }
    try {
      await apiClient.switchAccount(option.value!);
    } catch (error) {
      console.warn('Account switch warmup failed:', error);
    }
  };

  const handleAccountAdded = () => {
    // Refresh account list after adding new account
    fetchAccounts();
  };

  const handleAccountDeleted = () => {
    // Refresh account list after deleting account
    fetchAccounts();
  };

  const handleRefreshAccounts = async () => {
    // Two-step refresh-all flow:
    // 1. Call refresh-all to re-assume STS roles and update Secrets Manager entries.
    // 2. Re-fetch the account list so the selector shows updated statuses.
    // Both steps run even if the first partially fails (some accounts may refresh OK).
    setIsLoadingAccounts(true);
    setRefreshError('');
    try {
      console.log('Refreshing all account tokens...');
      const refreshResult = await apiClient.refreshAllAccounts();
      
      if (refreshResult.success) {
        console.log(`Refreshed ${refreshResult.refreshed} account(s), ${refreshResult.failed} failed`);
        
        // Show detailed error information if any accounts failed
        if (refreshResult.failed > 0 && refreshResult.results) {
          const failedAccounts = refreshResult.results.filter((r: any) => r.status === 'failed' || r.status === 'error');
          if (failedAccounts.length > 0) {
            const errorDetails = failedAccounts.map((r: any) => 
              `• ${r.account}: ${r.error || r.message}`
            ).join('\n');
            
            // Show alert with error details
            setRefreshError(`Refresh completed with ${refreshResult.failed} failure(s):\n\n${errorDetails}\n\nCommon causes:\n- IAM role trust policy doesn't match current MSP principal\n- External ID mismatch\n- IAM role doesn't exist in customer account`);
          }
        }
      }
      
      // Now fetch updated account list
      await fetchAccounts();
    } catch (error) {
      console.error('Account refresh failed:', error);
      // Still try to fetch accounts even if refresh failed
      await fetchAccounts();
    } finally {
      setIsLoadingAccounts(false);
    }
  };

  const handleApprovalProcessed = () => {
    // Callback when approval is processed
    console.log('Approval processed');
  };

  // Get customer accounts count for delete button
  const customerAccounts = (accounts ?? []).filter(acc => acc.type === 'customer');

  return (
    <SpaceBetween size="l">
      {/* Account Section */}
      <Container>
        <SpaceBetween size="m">
          <Select
            selectedOption={currentOption}
            options={accountOptions}
            onChange={({ detail }) => handleAccountChange(detail.selectedOption)}
            placeholder={isLoadingAccounts ? "Loading accounts..." : "Select AWS account"}
          />
          <SpaceBetween size="xs">
            <SpaceBetween direction="horizontal" size="xs">
              <Button 
                iconName="add-plus" 
                variant="normal"
                onClick={() => setShowAddAccountModal(true)}
              >
                Add
              </Button>
              <Button 
                iconName="remove" 
                variant="normal"
                onClick={() => setShowDeleteAccountModal(true)}
                disabled={customerAccounts.length === 0}
              >
                Delete
              </Button>
              <Button 
                iconName="refresh" 
                variant="normal"
                onClick={handleRefreshAccounts}
                loading={isLoadingAccounts}
              >
                Refresh
              </Button>
            </SpaceBetween>
          </SpaceBetween>
          {refreshError && <Alert type="warning" dismissible onDismiss={() => setRefreshError('')}>{refreshError}</Alert>}
          {selectedAccount && (
            <Alert type="info">
              Currently using: {selectedAccount.name}
              {selectedAccount.type === 'customer' && (
                <>
                  <br />
                  <StatusIndicator type={selectedAccount.status === 'active' ? 'success' : 'warning'}>
                    {selectedAccount.status}
                  </StatusIndicator>
                </>
              )}
            </Alert>
          )}
        </SpaceBetween>
      </Container>

      {/* Workflow Mode with Approvals */}
      <SpaceBetween size="m">
        <WorkflowSelector />
        <ApprovalCards onApprovalProcessed={handleApprovalProcessed} />
      </SpaceBetween>

      {/* Sample Questions */}
      <SampleQuestions onQuestionClick={setInputValue} />

      {/* Health Dashboard */}
      <HealthDashboard />

      {/* User Info & Actions */}
      <Container header={<Header variant="h3">User</Header>}>
        <SpaceBetween size="m">
          <Box variant="p">
            Signed in as: {user?.email || 'Unknown'}
          </Box>
          <Button 
            fullWidth 
            variant="normal" 
            onClick={signOut}
          >
            Sign out
          </Button>
        </SpaceBetween>
      </Container>

      {/* Clear Chat */}
      <Button 
        fullWidth 
        iconName="delete-marker" 
        variant="normal"
        onClick={clearMessages}
      >
        Clear chat
      </Button>

      {/* Add Account Modal */}
      <AddAccountModal
        visible={showAddAccountModal}
        onDismiss={() => setShowAddAccountModal(false)}
        onAccountAdded={handleAccountAdded}
      />

      {/* Delete Account Modal */}
      <DeleteAccountModal
        visible={showDeleteAccountModal}
        accounts={accounts}
        onDismiss={() => setShowDeleteAccountModal(false)}
        onAccountDeleted={handleAccountDeleted}
      />
    </SpaceBetween>
  );
}
