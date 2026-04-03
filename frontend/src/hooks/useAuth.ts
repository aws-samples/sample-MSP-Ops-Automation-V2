// frontend/src/hooks/useAuth.ts
/**
 * Hook for consuming the authentication context.
 *
 * Re-exports everything from `AuthContext` (user, tokens, loading,
 * isAuthenticated, signIn, completePasswordChange, signOut) as a single
 * convenient hook.
 *
 * Throws a descriptive error if called outside of `AuthProvider` so
 * misconfigured route trees are caught immediately during development rather
 * than silently returning undefined values.
 *
 * @returns The full `AuthContextType` value provided by `AuthProvider`.
 * @throws {Error} If the hook is used outside of an `AuthProvider` tree.
 */

import { useContext } from 'react';
import { AuthContext } from '../components/auth/AuthProvider';

export function useAuth() {
  const context = useContext(AuthContext);
  
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  
  return context;
}
