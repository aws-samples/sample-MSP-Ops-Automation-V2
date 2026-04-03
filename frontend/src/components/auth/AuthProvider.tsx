// frontend/src/components/auth/AuthProvider.tsx
/**
 * Authentication context provider.
 *
 * Wraps the application and exposes `AuthContext` with the following shape:
 * - `user` / `tokens` — current authenticated user and JWTs (in-memory only).
 * - `loading` — true while the initial session restore is in flight; guards
 *   `ProtectedRoute` from redirecting before auth state is known.
 * - `isAuthenticated` — true when both `user` and `tokens` are non-null.
 * - `signIn(email, password)` — delegates to `cognitoService.signIn`, then
 *   persists the refresh token as an httpOnly cookie via `POST /auth/set-refresh`
 *   so it survives page reloads without ever touching localStorage.
 * - `completePasswordChange(cognitoUser, newPassword, userAttributes)` —
 *   handles the Cognito `NEW_PASSWORD_REQUIRED` challenge raised for
 *   admin-created accounts on first login.
 * - `signOut()` — clears in-memory tokens, clears the httpOnly cookie
 *   (best-effort), resets chat and workflow stores, then navigates to `/login`.
 *
 * Session restore strategy (on mount):
 * The provider calls `POST /auth/restore` with `withCredentials: true`.
 * The backend reads the httpOnly refresh cookie, exchanges it for fresh
 * Cognito tokens, and returns the user object. This means the user stays
 * logged in across hard reloads without storing tokens in JS-accessible
 * storage (XSS mitigation).
 *
 * Token auto-refresh runs on a 50-minute interval while `tokens` is set,
 * using the same `/auth/restore` endpoint. If the restore fails (e.g. cookie
 * expired), `signOut` is called automatically.
 *
 * @param children - React subtree that needs access to auth state.
 */

import React, { createContext, useState, useEffect, useCallback, ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import * as cognitoService from '../../services/auth/cognitoService';
import { apiClient } from '../../services/api/apiClient';
import { useChatStore } from '../../store/chatStore';
import { useWorkflowStore } from '../../store/workflowStore';
import type { User, AuthTokens } from '../../types';

const AUTH_API = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1');

/** POST the refresh token to the backend once so it's stored as an httpOnly cookie. */
async function persistRefreshCookie(refreshToken: string): Promise<void> {
  await axios.post(`${AUTH_API}/auth/set-refresh`, { refresh_token: refreshToken }, { withCredentials: true });
}

/** Restore a session from the httpOnly cookie (called on page load). */
async function restoreSessionFromCookie(): Promise<{ user: User; tokens: AuthTokens } | null> {
  try {
    const res = await axios.post(`${AUTH_API}/auth/restore`, {}, { withCredentials: true });
    if (res.data.success) {
      return {
        user: res.data.user as User,
        tokens: { idToken: res.data.idToken, accessToken: res.data.accessToken, refreshToken: '' },
      };
    }
    return null;
  } catch {
    return null;
  }
}

/** Clear the httpOnly cookie on sign-out. */
async function clearRefreshCookie(): Promise<void> {
  try {
    await axios.post(`${AUTH_API}/auth/logout`, {}, { withCredentials: true });
  } catch { /* best-effort */ }
}

interface AuthContextType {
  user: User | null;
  tokens: AuthTokens | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  completePasswordChange: (cognitoUser: any, newPassword: string, userAttributes: any) => Promise<void>;
  signOut: () => void;
  isAuthenticated: boolean;
}

export const AuthContext = createContext<AuthContextType | undefined>(undefined);

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [tokens, setTokens] = useState<AuthTokens | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  // Restore session on mount via httpOnly cookie (no localStorage read)
  useEffect(() => {
    const checkAuth = async () => {
      try {
        const session = await restoreSessionFromCookie();
        if (session) {
          setUser(session.user);
          setTokens(session.tokens);
          cognitoService.saveTokens(session.tokens);
          cognitoService.saveUser(session.user);
        }
      } catch (error) {
        console.error('Auth restore failed:', error);
      } finally {
        setLoading(false);
      }
    };

    checkAuth();
  }, []);

  // Auto-refresh tokens every 50 minutes via httpOnly cookie (no localStorage)
  useEffect(() => {
    if (!tokens) return;

    const refreshInterval = setInterval(async () => {
      try {
        const session = await restoreSessionFromCookie();
        if (session) {
          setTokens(session.tokens);
          cognitoService.saveTokens(session.tokens);
        } else {
          handleSignOut();
        }
      } catch (error) {
        console.error('Token refresh failed:', error);
        handleSignOut();
      }
    }, 50 * 60 * 1000); // 50 minutes

    return () => clearInterval(refreshInterval);
  }, [tokens]);

  const handleSignIn = useCallback(async (email: string, password: string) => {
    setLoading(true);
    try {
      const result = await cognitoService.signIn(email, password);
      setUser(result.user);
      setTokens(result.tokens);
      cognitoService.saveTokens(result.tokens);
      cognitoService.saveUser(result.user);
      // Persist refresh token in httpOnly cookie — removes it from JS memory
      await persistRefreshCookie(result.tokens.refreshToken).catch(e =>
        console.warn('Failed to persist refresh cookie:', e)
      );
      navigate('/app');
    } catch (error: any) {
      if (error.name === 'NewPasswordRequired') {
        throw error; // Let SignInPage handle password change
      }
      throw error;
    } finally {
      setLoading(false);
    }
  }, [navigate]);

  const handleCompletePasswordChange = useCallback(async (
    cognitoUser: any,
    newPassword: string,
    userAttributes: any
  ) => {
    setLoading(true);
    try {
      const result = await cognitoService.completeNewPassword(cognitoUser, newPassword, userAttributes);
      setUser(result.user);
      setTokens(result.tokens);
      cognitoService.saveTokens(result.tokens);
      cognitoService.saveUser(result.user);
      // Persist refresh token in httpOnly cookie before navigating (critical for session restore on reload)
      await persistRefreshCookie(result.tokens.refreshToken).catch(e =>
        console.warn('Failed to persist refresh cookie after password change:', e)
      );
      navigate('/app');
    } catch (error: any) {
      throw error;
    } finally {
      setLoading(false);
    }
  }, [navigate]);

  const handleSignOut = useCallback(() => {
    cognitoService.signOut();
    clearRefreshCookie(); // best-effort, don't await
    useChatStore.getState().clearMessages();
    useWorkflowStore.getState().closeWorkflowPanel();
    useWorkflowStore.getState().setPendingWorkflowId(null);
    setUser(null);
    setTokens(null);
    navigate('/login');
  }, [navigate]);

  const value: AuthContextType = {
    user,
    tokens,
    loading,
    signIn: handleSignIn,
    completePasswordChange: handleCompletePasswordChange,
    signOut: handleSignOut,
    isAuthenticated: !!user && !!tokens,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
