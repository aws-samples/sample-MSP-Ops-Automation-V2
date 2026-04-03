import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './components/auth/AuthProvider';
import { ProtectedRoute } from './components/auth/ProtectedRoute';
import SignInPage from './pages/SignInPage';
import MainAppPage from './pages/MainAppPage';

/**
 * Root application component.
 *
 * Sets up the React Router tree and wraps all routes in `AuthProvider` so that
 * authentication state is available everywhere in the tree.
 *
 * Route structure:
 * - `/login`  — public sign-in page (no auth required)
 * - `/app`    — main chat/dashboard page, guarded by `ProtectedRoute`
 * - `/` and any unknown path — redirect to `/app` (which then redirects to
 *   `/login` if the user is not authenticated)
 */
function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<SignInPage />} />
          
          {/* Protected routes */}
          <Route 
            path="/app" 
            element={
              <ProtectedRoute>
                <MainAppPage />
              </ProtectedRoute>
            } 
          />
          
          {/* Default redirect to app (will redirect to login if not authenticated) */}
          <Route path="/" element={<Navigate to="/app" replace />} />
          <Route path="*" element={<Navigate to="/app" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
