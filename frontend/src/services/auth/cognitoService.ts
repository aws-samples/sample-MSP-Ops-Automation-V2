// frontend/src/services/auth/cognitoService.ts
/**
 * Cognito authentication service — low-level Cognito SDK wrapper.
 *
 * Provides sign-in, sign-out, password-change, and token management backed by
 * `amazon-cognito-identity-js`. All token storage is kept in memory rather than
 * localStorage to reduce XSS exposure:
 *
 * - **`_mem`** — module-level object holding the current `idToken`,
 *   `accessToken`, and `User`. Cleared on sign-out.
 * - **`cognitoStorage`** — custom storage adapter passed to the SDK that
 *   redirects all SDK reads/writes to a plain in-memory `Record<string,string>`
 *   (`_sdkStore`) instead of `window.localStorage`.
 *
 * The refresh token is **never** stored here. It is sent to the backend
 * immediately after sign-in/password-change and lives exclusively in an
 *  httpOnly cookie managed by the server. `saveTokens` and `getStoredTokens`
 * therefore always return an empty string for `refreshToken`.
 *
 * Key exports:
 * - `signIn` — authenticates with SRP; rejects with a `NewPasswordRequired`
 *   shaped error object if Cognito requires a password change on first login.
 * - `completeNewPassword` — resolves the `NEW_PASSWORD_REQUIRED` challenge.
 *   Strips non-modifiable attributes (`email`, `email_verified`,
 *   `phone_number_verified`, `sub`) before calling the SDK to avoid a Cognito
 *   `NotAuthorizedException`.
 * - `signOut` — calls the SDK sign-out, then clears both `_mem` and `_sdkStore`.
 * - `getInMemoryTokens` — returns a shallow copy of `_mem` tokens for use by
 *   `apiClient` request interceptors.
 * - `saveTokens` / `getStoredTokens` / `saveUser` / `getStoredUser` — simple
 *   in-memory persistence helpers called by `AuthProvider`.
 */

import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
  CognitoUserSession,
  CognitoUserAttribute,
} from 'amazon-cognito-identity-js';
import type { AuthTokens, User } from '../../types';

// ---------------------------------------------------------------------------
// In-memory token store — XSS-safe replacement for localStorage
// ---------------------------------------------------------------------------
interface InMemoryTokens {
  idToken: string | null;
  accessToken: string | null;
  user: import('../../types').User | null;
}

const _mem: InMemoryTokens = { idToken: null, accessToken: null, user: null };

/** Read the live in-memory tokens (used by apiClient interceptors). Returns a shallow copy to prevent external mutation. */
export const getInMemoryTokens = () => ({ idToken: _mem.idToken, accessToken: _mem.accessToken });

// ---------------------------------------------------------------------------
// Custom CognitoUserPool storage — prevents amazon-cognito-identity-js from
// writing tokens to localStorage. Uses the same in-memory map above.
// ---------------------------------------------------------------------------
const _sdkStore: Record<string, string> = {};
const cognitoStorage = {
  setItem:    (k: string, v: string) => { _sdkStore[k] = v; },
  getItem:    (k: string) => _sdkStore[k] ?? null,
  removeItem: (k: string) => { delete _sdkStore[k]; },
  clear:      () => { Object.keys(_sdkStore).forEach(k => delete _sdkStore[k]); },
};

// Cognito configuration from environment
const poolData = {
  UserPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID,
  ClientId: import.meta.env.VITE_COGNITO_CLIENT_ID,
  Storage: cognitoStorage,
};

const userPool = new CognitoUserPool(poolData);

/**
 * Sign in with email and password
 */
export const signIn = async (
  email: string,
  password: string
): Promise<{ user: User; tokens: AuthTokens; requiresPasswordChange?: boolean }> => {
  return new Promise((resolve, reject) => {
    const authenticationDetails = new AuthenticationDetails({
      Username: email,
      Password: password,
    });

    const cognitoUser = new CognitoUser({
      Username: email,
      Pool: userPool,
      Storage: cognitoStorage,
    });

    cognitoUser.authenticateUser(authenticationDetails, {
      onSuccess: (session: CognitoUserSession) => {
        const tokens = extractTokens(session);
        const user = extractUserFromSession(session);
        resolve({ user, tokens });
      },

      onFailure: (err) => {
        reject(new Error(err.message || 'Authentication failed'));
      },

      newPasswordRequired: (userAttributes) => {
        // First time login - password change required
        reject({
          name: 'NewPasswordRequired',
          message: 'Password change required',
          cognitoUser,
          userAttributes,
        });
      },
    });
  });
};

/**
 * Complete new password challenge
 */
export const completeNewPassword = async (
  cognitoUser: CognitoUser,
  newPassword: string,
  userAttributes: any
): Promise<{ user: User; tokens: AuthTokens }> => {
  return new Promise((resolve, reject) => {
    // Only include attributes that can be modified
    // Email cannot be modified after account creation
    const attributesToUpdate: any = {};
    
    // Copy only modifiable attributes (explicitly exclude email-related ones)
    for (const key in userAttributes) {
      if (key !== 'email' && 
          key !== 'email_verified' && 
          key !== 'phone_number_verified' &&
          key !== 'sub') {
        attributesToUpdate[key] = userAttributes[key];
      }
    }

    cognitoUser.completeNewPasswordChallenge(
      newPassword,
      attributesToUpdate,
      {
        onSuccess: (session: CognitoUserSession) => {
          const tokens = extractTokens(session);
          const user = extractUserFromSession(session);
          resolve({ user, tokens });
        },
        onFailure: (err) => {
          reject(new Error(err.message || 'Password change failed'));
        },
      }
    );
  });
};

/**
 * Sign out current user
 */
export const signOut = (): void => {
  const cognitoUser = userPool.getCurrentUser();
  if (cognitoUser) {
    cognitoUser.signOut();
  }
  // Clear in-memory tokens and SDK store
  _mem.idToken = null;
  _mem.accessToken = null;
  _mem.user = null;
  cognitoStorage.clear();
};

/**
 * Get current authenticated user
 */
export const getCurrentUser = async (): Promise<{
  user: User;
  tokens: AuthTokens;
} | null> => {
  const cognitoUser = userPool.getCurrentUser();

  if (!cognitoUser) {
    return null;
  }

  return new Promise((resolve, reject) => {
    cognitoUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session || !session.isValid()) {
        resolve(null);
        return;
      }

      const tokens = extractTokens(session);
      const user = extractUserFromSession(session);
      resolve({ user, tokens });
    });
  });
};

/**
 * Refresh authentication tokens
 */
export const refreshSession = async (): Promise<AuthTokens> => {
  const cognitoUser = userPool.getCurrentUser();

  if (!cognitoUser) {
    throw new Error('No user session found');
  }

  return new Promise((resolve, reject) => {
    cognitoUser.getSession((err: Error | null, session: CognitoUserSession | null) => {
      if (err || !session) {
        reject(err || new Error('Failed to refresh session'));
        return;
      }

      if (!session.isValid()) {
        // Try to refresh
        const refreshToken = session.getRefreshToken();
        cognitoUser.refreshSession(refreshToken, (refreshErr, newSession) => {
          if (refreshErr || !newSession) {
            reject(refreshErr || new Error('Token refresh failed'));
            return;
          }

          const tokens = extractTokens(newSession);
          saveTokens(tokens);
          resolve(tokens);
        });
      } else {
        const tokens = extractTokens(session);
        resolve(tokens);
      }
    });
  });
};

/**
 * Extract tokens from session
 */
const extractTokens = (session: CognitoUserSession): AuthTokens => {
  return {
    idToken: session.getIdToken().getJwtToken(),
    accessToken: session.getAccessToken().getJwtToken(),
    refreshToken: session.getRefreshToken().getToken(),
  };
};

/**
 * Extract user info from session
 */
const extractUserFromSession = (session: CognitoUserSession): User => {
  const idToken = session.getIdToken();
  const payload = idToken.payload;

  return {
    userId: payload.sub,
    email: payload.email || payload['cognito:username'],
    username: payload['cognito:username'] || payload.email,
  };
};

/**
 * Save tokens to in-memory store (no localStorage).
 * The refresh token is intentionally excluded — it lives only in the httpOnly cookie.
 */
export const saveTokens = (tokens: AuthTokens): void => {
  _mem.idToken = tokens.idToken;
  _mem.accessToken = tokens.accessToken;
  // refresh token is NOT stored in memory — only in the httpOnly cookie
};

/**
 * Get tokens from in-memory store.
 * refreshToken is always empty string here; the cookie carries it server-side.
 */
export const getStoredTokens = (): AuthTokens | null => {
  if (!_mem.idToken || !_mem.accessToken) return null;
  return { idToken: _mem.idToken, accessToken: _mem.accessToken, refreshToken: '' };
};

/**
 * Save user to in-memory store (no localStorage).
 */
export const saveUser = (user: User): void => {
  _mem.user = user;
};

/**
 * Get user from in-memory store.
 */
export const getStoredUser = (): User | null => {
  return _mem.user;
};
