// frontend/src/store/accountStore.ts
/**
 * Account selection state for the MSP assistant.
 *
 * Tracks which AWS account the user is currently operating against.  The store
 * is initialised with the default MSP account so that the UI is immediately
 * usable without waiting for the account list to load from the backend.
 *
 * `accounts` is populated by `NavigationPanel` on mount via `apiClient.getAccounts()`.
 * `selectedAccount` is kept in sync with the Cloudscape Select component and is
 * also sent as `account_name` on every chat request so the backend targets the
 * correct AWS account context.
 */

import { create } from 'zustand';
import type { Account } from '../types';

interface AccountStoreState {
  /** Full list of accounts returned by the backend (MSP account + all onboarded customer accounts). */
  accounts: Account[];
  /** The account currently targeted by chat requests and AWS API calls. Never null in normal operation — falls back to the default MSP account. */
  selectedAccount: Account | null;

  /** Replace the full account list (called after load or after add/delete). */
  setAccounts: (accounts: Account[]) => void;
  /** Update the active account selection and propagate to backend via `apiClient.switchAccount`. */
  setSelectedAccount: (account: Account | null) => void;
}

export const useAccountStore = create<AccountStoreState>((set) => ({
  accounts: [],
  // Pre-seed with the MSP account so the Select dropdown is not empty on first render,
  // before the async getAccounts() call in NavigationPanel resolves.
  selectedAccount: {
    id: 'default',
    name: 'Default (Current MSP)',
    type: 'msp',
    status: 'active'
  },
  
  setAccounts: (accounts) => set({ accounts }),
  setSelectedAccount: (account) => set({ selectedAccount: account }),
}));
