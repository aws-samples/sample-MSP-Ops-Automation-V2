import React, { useState } from 'react';
import { useAuth } from '../hooks/useAuth';
import AppLayoutToolbar from '@cloudscape-design/components/app-layout';
import Form from '@cloudscape-design/components/form';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import Alert from '@cloudscape-design/components/alert';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Link from '@cloudscape-design/components/link';

interface FormData {
  email: string;
  password: string;
}

interface FormErrors {
  email: string;
  password: string;
}

/**
 * Cognito sign-in page.
 *
 * Renders a Cloudscape `AppLayout` (navigation and tools panels hidden) with
 * a centered form that handles two distinct states:
 *
 * 1. **Normal sign-in** — email + password fields with inline validation.
 *    On success, `AuthProvider.signIn` stores tokens and navigates to `/app`.
 *
 * 2. **First-time password change** — shown when Cognito raises
 *    `NewPasswordRequired` (e.g., admin-created accounts). The user must set
 *    a new password (≥ 8 chars) before gaining access. The `cognitoUser`
 *    object and `userAttributes` returned by the challenge are kept in local
 *    state and forwarded to `completePasswordChange`.
 *
 * Validation is intentionally shallow (presence + email regex) — Cognito
 * performs authoritative policy enforcement server-side.
 *
 * Forgot-password flow is stubbed (`handleForgotPassword` logs a warning)
 * and is not yet implemented.
 */
function SignInPage() {
  const [formData, setFormData] = useState<FormData>({
    email: '',
    password: ''
  });

  const [formErrors, setFormErrors] = useState<FormErrors>({
    email: '',
    password: ''
  });

  const validateEmail = (email: string): string => {
    if (!email) {
      return 'Email address is required.';
    }
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(email)) {
      return 'Please use a valid email format.';
    }
    return '';
  };

  const validatePassword = (password: string): string => {
    if (!password) {
      return 'Password is required.';
    }
    return '';
  };

  const validateForm = (): boolean => {
    const emailError = validateEmail(formData.email);
    const passwordError = validatePassword(formData.password);

    const errors: FormErrors = {
      email: emailError,
      password: passwordError
    };

    setFormErrors(errors);

    if (emailError || passwordError) {
      console.log('Validation errors:', errors);
      return false;
    }

    return true;
  };

  const [passwordChangeRequired, setPasswordChangeRequired] = useState(false);
  const [newPasswordData, setNewPasswordData] = useState({ password: '', confirm: '' });
  const [cognitoUser, setCognitoUser] = useState<any>(null);
  const [userAttributes, setUserAttributes] = useState<any>(null);
  const [signInError, setSignInError] = useState('');

  const { signIn, completePasswordChange } = useAuth();

  const handleSignIn = async () => {
    if (!validateForm()) return;

    setSignInError('');
    try {
      await signIn(formData.email, formData.password);
      // User will be redirected by AuthProvider
    } catch (error: any) {
      if (error.name === 'NewPasswordRequired') {
        // First time login - need to change password
        setPasswordChangeRequired(true);
        setCognitoUser(error.cognitoUser);
        setUserAttributes(error.userAttributes);
      } else {
        setSignInError(error.message || 'Authentication failed');
      }
    }
  };

  const handlePasswordChange = async () => {
    if (newPasswordData.password !== newPasswordData.confirm) {
      setSignInError('Passwords do not match');
      return;
    }

    if (newPasswordData.password.length < 8) {
      setSignInError('Password must be at least 8 characters');
      return;
    }

    setSignInError('');
    try {
      // Remove attributes that cannot be modified
      const attributesToUpdate = { ...userAttributes };
      delete attributesToUpdate.email;
      delete attributesToUpdate.email_verified;
      delete attributesToUpdate.phone_number_verified;

      // Delegates to AuthProvider: saves tokens, persists httpOnly cookie, navigates to /app
      await completePasswordChange(cognitoUser, newPasswordData.password, attributesToUpdate);
    } catch (error: any) {
      setSignInError(error.message || 'Password change failed');
    }
  };

  const handleCancel = () => {
    setFormData({
      email: '',
      password: ''
    });
    setFormErrors({
      email: '',
      password: ''
    });
  };

  const handleForgotPassword = () => {
    console.log('Forgot password clicked');
    setSignInError('Forgot password flow is not yet implemented.');
  };

  return (
    <AppLayoutToolbar
      content={
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleCancel}>
                Cancel
              </Button>
              <Button 
                variant="primary" 
                onClick={passwordChangeRequired ? handlePasswordChange : handleSignIn}
              >
                {passwordChangeRequired ? 'Change password' : 'Sign in'}
              </Button>
            </SpaceBetween>
          }
          header={
            <Header 
              description="Sign in to your account to continue." 
              variant="h1"
            >
              AWS MSP Smart Agent Assist
            </Header>
          }
        >
          <Container
            header={
              <Header
                description="Enter your credentials to authenticate with Amazon Cognito."
                variant="h2"
              >
                Account credentials
              </Header>
            }
          >
              <SpaceBetween size="l">
                {signInError && (
                  <Alert type="error" dismissible onDismiss={() => setSignInError('')}>
                    {signInError}
                  </Alert>
                )}
                
                {!passwordChangeRequired && (
                  <Alert type="info">
                    Don't have an account? Contact your administrator to request access.
                  </Alert>
                )}
                
                {passwordChangeRequired && (
                  <Alert type="warning" header="Password change required">
                    This is your first sign-in. Please create a new password.
                  </Alert>
                )}
                
                {!passwordChangeRequired ? (
                  <>
              <FormField
                constraintText="Use a valid email format."
                description="Enter the email address associated with your account."
                label="Email address"
                errorText={formErrors.email}
              >
                <Input
                  placeholder="user@example.com"
                  type="email"
                  value={formData.email}
                  onChange={({ detail }) => {
                    setFormData({ ...formData, email: detail.value });
                    if (formErrors.email) {
                      setFormErrors({ ...formErrors, email: '' });
                    }
                  }}
                />
              </FormField>
              <FormField
                constraintText="Password is case sensitive."
                description="Enter your account password."
                label="Password"
                errorText={formErrors.password}
              >
                <Input
                  placeholder="Enter your password"
                  type="password"
                  value={formData.password}
                  onChange={({ detail }) => {
                    setFormData({ ...formData, password: detail.value });
                    if (formErrors.password) {
                      setFormErrors({ ...formErrors, password: '' });
                    }
                  }}
                />
              </FormField>
                    <SpaceBetween direction="horizontal" size="xs">
                      <Link variant="primary" onFollow={handleForgotPassword}>
                        Forgot password?
                      </Link>
                    </SpaceBetween>
                  </>
                ) : (
                  <>
                    <FormField
                      label="New password"
                      description="Must be at least 8 characters"
                      constraintText="Password requirements: Min 8 characters"
                    >
                      <Input
                        placeholder="Enter new password"
                        type="password"
                        value={newPasswordData.password}
                        onChange={({ detail }) => 
                          setNewPasswordData({ ...newPasswordData, password: detail.value })
                        }
                      />
                    </FormField>
                    <FormField
                      label="Confirm new password"
                    >
                      <Input
                        placeholder="Confirm new password"
                        type="password"
                        value={newPasswordData.confirm}
                        onChange={({ detail }) => 
                          setNewPasswordData({ ...newPasswordData, confirm: detail.value })
                        }
                      />
                    </FormField>
                  </>
                )}
            </SpaceBetween>
          </Container>
        </Form>
      }
      contentType="form"
      navigationHide
      toolsHide
    />
  );
}

export default SignInPage;
