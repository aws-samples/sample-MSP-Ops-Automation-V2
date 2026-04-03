// frontend/src/components/auth/ProtectedRoute.tsx
/**
 * Route guard for authenticated pages.
 *
 * Reads `isAuthenticated` and `loading` from `AuthContext` (via `useAuth`)
 * and handles three states:
 *
 * 1. **Loading** — `AuthProvider` is still restoring the session from the
 *    httpOnly cookie. Renders a centered `Spinner` to prevent a flash of the
 *    login page for users who are already authenticated.
 *
 * 2. **Not authenticated** — Redirects to `/login` using React Router's
 *    `<Navigate replace>` so the login page does not appear in the browser
 *    history as a forward-navigation target.
 *
 * 3. **Authenticated** — Renders the protected `children` unchanged.
 *
 * @param children - The single route element to render when authenticated.
 */

import React, { ReactElement } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';
import Spinner from '@cloudscape-design/components/spinner';
import Container from '@cloudscape-design/components/container';
import Box from '@cloudscape-design/components/box';

interface ProtectedRouteProps {
  children: ReactElement;
}

export function ProtectedRoute({ children }: ProtectedRouteProps) {
  const { isAuthenticated, loading } = useAuth();

  if (loading) {
    return (
      <Box textAlign="center" padding={{ vertical: 'xxxl' }}>
        <Spinner size="large" />
        <Box variant="p" padding={{ top: 'm' }}>
          Loading...
        </Box>
      </Box>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return children;
}
