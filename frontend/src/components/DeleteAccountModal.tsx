// frontend/src/components/DeleteAccountModal.tsx
/**
 * Delete Customer Account confirmation modal.
 *
 * Presents a dropdown of existing customer accounts (MSP/system accounts are
 * filtered out — only `type === 'customer'` accounts are selectable) and
 * requires the operator to explicitly choose one before the delete button
 * becomes active.
 *
 * Once confirmed, calls `apiClient.deleteAccount` which removes the account
 * record and its associated Secrets Manager entry from the backend. The
 * operation cannot be undone, which is communicated via a persistent warning
 * `Alert`.
 *
 * State is fully reset on close so the modal starts clean the next time it
 * is opened.
 *
 * @param visible - Controls modal visibility.
 * @param accounts - Full list of accounts from the account store; the
 *   component filters to customer accounts internally.
 * @param onDismiss - Called when the modal is dismissed without deleting.
 * @param onAccountDeleted - Called after successful deletion so the parent
 *   can refresh the account list.
 */

import React, { useState } from 'react';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Select, { SelectProps } from '@cloudscape-design/components/select';
import Alert from '@cloudscape-design/components/alert';
import { apiClient } from '../services/api/apiClient';

interface DeleteAccountModalProps {
  visible: boolean;
  accounts: any[];
  onDismiss: () => void;
  onAccountDeleted: () => void;
}

export function DeleteAccountModal({ 
  visible, 
  accounts, 
  onDismiss, 
  onAccountDeleted 
}: DeleteAccountModalProps) {
  const [selectedAccount, setSelectedAccount] = useState<SelectProps.Option | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [error, setError] = useState<string>('');

  // Filter to only customer accounts
  const customerAccounts = (accounts ?? []).filter(acc => acc.type === 'customer');
  
  const accountOptions: SelectProps.Options = (customerAccounts ?? []).map(account => ({
    label: `${account.name} (${account.account_id})`,
    value: account.id
  }));

  const handleDelete = async () => {
    if (!selectedAccount?.value) return;

    setIsDeleting(true);
    setError('');

    try {
      const result = await apiClient.deleteAccount(selectedAccount.value);
      
      if (result.success) {
        onAccountDeleted();
        handleClose();
      } else {
        setError(result.message || 'Failed to delete account');
      }
    } catch (err: any) {
      console.error('Delete account failed:', err);
      setError(err.message || 'Failed to delete account');
    } finally {
      setIsDeleting(false);
    }
  };

  const handleClose = () => {
    setSelectedAccount(null);
    setError('');
    onDismiss();
  };

  const selectedAccountData = selectedAccount 
    ? customerAccounts.find(acc => acc.id === selectedAccount.value)
    : null;

  return (
    <Modal
      visible={visible}
      onDismiss={handleClose}
      header="Delete Customer Account"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={handleClose}>Cancel</Button>
            <Button 
              variant="primary" 
              onClick={handleDelete}
              disabled={!selectedAccount || isDeleting}
              loading={isDeleting}
            >
              Confirm Deletion
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {customerAccounts.length === 0 ? (
          <Alert type="info">
            No customer accounts to delete.
          </Alert>
        ) : (
          <>
            <Alert type="warning">
              <strong>Warning:</strong> This action cannot be undone.
            </Alert>

            <FormField label="Select account to delete">
              <Select
                selectedOption={selectedAccount}
                options={accountOptions}
                onChange={({ detail }) => setSelectedAccount(detail.selectedOption)}
                placeholder="Choose an account to delete"
              />
            </FormField>

            {selectedAccountData && (
              <Alert type="info">
                <Box>
                  <Box fontWeight="bold">
                    You are about to delete: {selectedAccountData.name}
                  </Box>
                  <Box margin={{ top: 's' }}>
                    <strong>Account ID:</strong> {selectedAccountData.account_id}
                  </Box>
                </Box>
                <Box margin={{ top: 'm' }}>
                  <Box fontWeight="bold">This will:</Box>
                  <Box>• Remove account from your MSP dashboard</Box>
                  <Box>• Delete stored STS tokens from Secrets Manager</Box>
                  <Box>• Stop all monitoring and access to this customer account</Box>
                </Box>
              </Alert>
            )}

            {error && (
              <Alert type="error">
                {error}
              </Alert>
            )}
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
}
