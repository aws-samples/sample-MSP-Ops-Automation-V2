// frontend/src/components/AddAccountModal.tsx
/**
 * Add Customer Account modal — 3-step wizard.
 *
 * **Step 1 — Customer Info:** Accepts a free-text customer name, sanitizes it
 * to snake_case on the fly (shown as a preview), then calls
 * `apiClient.prepareAccount` to pre-register the account on the backend and
 * receive a backend-generated `role_name` and `external_id`. This ensures the
 * role name and external ID are always consistent with the backend's records.
 *
 * **Step 2 — Setup Instructions:** Displays ready-to-run AWS CLI commands
 * (pre-populated with the MSP principal ARN fetched from the backend) that the
 * customer must execute in their own AWS account to create the cross-account
 * IAM role. A `CopyButton` helper provides one-click clipboard copy with
 * brief visual feedback for each command block.
 *
 * **Step 3 — Test Connection:** Collects the customer's 12-digit AWS account
 * ID, then calls `apiClient.createAccount` to perform an STS assume-role test.
 * If the role is not yet configured correctly the account is saved in `pending`
 * state with an STS error message surfaced to the operator.
 *
 * The MSP principal ARN is fetched on modal open (`visible` change) so the
 * Step 2 commands are always current. A fallback placeholder is used if the
 * fetch fails, allowing the wizard to proceed.
 *
 * @param visible - Controls modal visibility.
 * @param onDismiss - Called when the modal is closed without completing setup.
 * @param onAccountAdded - Called after a successful account creation so the
 *   parent can refresh the account list.
 */

import React, { useState, useEffect } from 'react';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Textarea from '@cloudscape-design/components/textarea';
import Alert from '@cloudscape-design/components/alert';
import Container from '@cloudscape-design/components/container';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Icon from '@cloudscape-design/components/icon';
import { apiClient } from '../services/api/apiClient';

interface AddAccountModalProps {
  visible: boolean;
  onDismiss: () => void;
  onAccountAdded: () => void;
}

// Copy button component with feedback
function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <Button
      iconName={copied ? 'status-positive' : 'copy'}
      variant="inline-icon"
      onClick={handleCopy}
      ariaLabel={label || 'Copy to clipboard'}
    >
      {copied ? 'Copied!' : ''}
    </Button>
  );
}

export function AddAccountModal({ visible, onDismiss, onAccountAdded }: AddAccountModalProps) {
  const [step, setStep] = useState(1);
  const [customerNameInput, setCustomerNameInput] = useState(''); // User input (original)
  const [customerName, setCustomerName] = useState(''); // Sanitized name (used everywhere)
  const [accountId, setAccountId] = useState('');
  const [description, setDescription] = useState('');
  const [roleName, setRoleName] = useState('');
  const [externalId, setExternalId] = useState('');
  const [mspPrincipalArn, setMspPrincipalArn] = useState('');
  const [mspAccountId, setMspAccountId] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  // Fetch MSP principal ARN when modal opens
  useEffect(() => {
    if (visible) {
      apiClient.getMspPrincipal()
        .then((result) => {
          if (result.success) {
            setMspPrincipalArn(result.principal_arn);
            setMspAccountId(result.account_id);
          }
        })
        .catch((err) => {
          console.error('Failed to fetch MSP principal:', err);
          // Fallback to placeholder
          setMspPrincipalArn('YOUR_MSP_PRINCIPAL_ARN');
        });
    }
  }, [visible]);

  const handleClose = () => {
    setStep(1);
    setCustomerNameInput('');
    setCustomerName('');
    setAccountId('');
    setDescription('');
    setRoleName('');
    setExternalId('');
    setError('');
    onDismiss();
  };

  const handleStep1Next = async () => {
    if (!customerNameInput.trim()) {
      return;
    }
    
    setIsLoading(true);
    
    try {
      // Sanitize name: lowercase, spaces/hyphens to underscores, remove special chars
      const sanitized = customerNameInput.toLowerCase().trim()
        .replace(/[\s\-]+/g, '_')  // spaces and hyphens to underscores
        .replace(/[^a-z0-9_]/g, ''); // keep only letters, numbers, underscores
      
      // Store sanitized name
      setCustomerName(sanitized);
      
      // Call backend to prepare account and get external_id/role_name
      const result = await apiClient.prepareAccount(sanitized);
      
      if (result.success) {
        // Use backend-generated values (ensures consistency)
        setRoleName(result.role_name);
        setExternalId(result.external_id);
        setMspPrincipalArn(result.msp_principal_arn);
        
        console.log('Account prepared:', result);
        setStep(2);
      } else {
        console.error('Account preparation failed:', result);
        // Could show error here
      }
    } catch (error) {
      console.error('Error preparing account:', error);
      // Could show error here
    } finally {
      setIsLoading(false);
    }
  };

  const handleStep3Complete = async () => {
    if (!accountId || accountId.length !== 12) {
      return;
    }

    setIsLoading(true);
    try {
      const result = await apiClient.createAccount({
        account_name: customerName,
        account_id: accountId,
        description: description || undefined
      });

      if (result.success) {
        console.log('Account created successfully:', result);
        
        // Check if account is pending with STS error
        if (result.account?.status === 'pending' && result.account?.sts_error) {
          setError(`Account created but pending activation\n\nSTS Error: ${result.account.sts_error}\n\nCommon causes:\n- IAM role trust policy doesn't match current MSP principal\n- External ID mismatch between role and secret\n- IAM role doesn't exist yet in customer account\n\nPlease verify the IAM role setup and click Refresh to retry.`);
        }

        onAccountAdded();
        handleClose();
      } else {
        console.error('Account creation failed:', result.message);
        setError(`Account creation failed:\n\n${result.message}`);
      }
    } catch (err: any) {
      console.error('Error adding account:', err);
      setError(`Error adding account:\n\n${err.response?.data?.detail || err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  // Generate CLI commands with actual MSP principal ARN
  const createRoleCommand = `aws iam create-role \\
  --role-name ${roleName} \\
  --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "${mspPrincipalArn}"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "${externalId}"
        }
      }
    }
  ]
}'`;

  const attachPolicyCommand = `aws iam put-role-policy \\
  --role-name ${roleName} \\
  --policy-name MSPCrossAccountPolicy \\
  --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudWatchReadOnly",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:DescribeAlarms",
        "cloudwatch:DescribeAlarmsForMetric",
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "cloudwatch:GetDashboard",
        "cloudwatch:ListDashboards"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudWatchLogsReadOnly",
      "Effect": "Allow",
      "Action": [
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:DescribeQueryDefinitions",
        "logs:GetLogEvents",
        "logs:FilterLogEvents",
        "logs:GetLogGroupFields",
        "logs:GetLogRecord",
        "logs:StartQuery",
        "logs:StopQuery",
        "logs:GetQueryResults"
      ],
      "Resource": "*"
    },
    {
      "Sid": "SecurityHubAccess",
      "Effect": "Allow",
      "Action": [
        "securityhub:GetFindings",
        "securityhub:ListFindings",
        "securityhub:BatchGetSecurityControls",
        "securityhub:GetEnabledStandards",
        "securityhub:DescribeStandards",
        "securityhub:DescribeStandardsControls",
        "securityhub:ListSecurityControlDefinitions",
        "securityhub:GetSecurityControlDefinition",
        "securityhub:BatchUpdateFindings"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CostExplorerReadOnly",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "ce:GetReservationUtilization",
        "ce:GetSavingsPlansUtilization",
        "ce:GetCostCategories",
        "ce:GetDimensionValues",
        "ce:GetRightsizingRecommendation",
        "ce:GetSavingsPlansPurchaseRecommendation",
        "ce:GetReservationPurchaseRecommendation",
        "ce:GetAnomalies"
      ],
      "Resource": "*"
    },
    {
      "Sid": "TrustedAdvisorReadOnly",
      "Effect": "Allow",
      "Action": [
        "support:DescribeTrustedAdvisorChecks",
        "support:DescribeTrustedAdvisorCheckResult",
        "support:DescribeTrustedAdvisorCheckSummaries",
        "support:RefreshTrustedAdvisorCheck"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2ReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus",
        "ec2:DescribeRegions",
        "ec2:DescribeAvailabilityZones"
      ],
      "Resource": "*"
    },
    {
      "Sid": "APIGatewayRemediation",
      "Effect": "Allow",
      "Action": [
        "apigateway:GET",
        "apigateway:PATCH",
        "apigateway:POST",
        "apigateway:PUT",
        "apigateway:DELETE",
        "apigateway:UpdateRestApiPolicy"
      ],
      "Resource": [
        "arn:aws:apigateway:*::/restapis",
        "arn:aws:apigateway:*::/restapis/*"
      ]
    },
    {
      "Sid": "ResourceDiscovery",
      "Effect": "Allow",
      "Action": [
        "tag:GetResources",
        "tag:GetTagKeys"
      ],
      "Resource": "*"
    },
    {
      "Sid": "STSGetCallerIdentity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}'`;

  const verifyRoleCommand = `aws iam get-role --role-name ${roleName}`;

  // Progress indicator
  const steps = ['Customer Info', 'Setup Instructions', 'Test Connection'];
  
  return (
    <Modal
      visible={visible}
      onDismiss={handleClose}
      header={`Add Customer Account - Step ${step} of 3`}
      size="large"
    >
      <SpaceBetween size="l">
        {/* Progress indicator */}
        <Box>
          <SpaceBetween direction="horizontal" size="m">
            {steps.map((stepName, index) => {
              const stepNum = index + 1;
              return (
                <Box key={stepNum} textAlign="center">
                  <Box
                    fontWeight={stepNum === step ? 'bold' : 'normal'}
                    color={stepNum === step ? 'text-status-info' : 
                           stepNum < step ? 'text-status-success' : 'text-body-secondary'}
                  >
                    {stepNum < step && ''}
                    {stepNum === step && ''}
                    {stepNum > step && ''}
                    {stepNum}. {stepName}
                  </Box>
                </Box>
              );
            })}
          </SpaceBetween>
        </Box>

        {/* Step 1: Customer Info */}
        {step === 1 && (
          <Container>
            <SpaceBetween size="m">
              <Box variant="h3">Customer Information</Box>
              <FormField label="Customer Name" description="Enter the display name (will be converted to snake_case)">
                <Input
                  value={customerNameInput}
                  onChange={({ detail }) => setCustomerNameInput(detail.value)}
                  placeholder="e.g., Acme Corporation"
                />
              </FormField>
              {customerNameInput && (
                <Alert type="info">
                  Will be saved as: <strong>{customerNameInput.toLowerCase().trim().replace(/[\s\-]+/g, '_').replace(/[^a-z0-9_]/g, '')}</strong>
                </Alert>
              )}
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={handleClose} disabled={isLoading}>Cancel</Button>
                <Button 
                  variant="primary" 
                  onClick={handleStep1Next}
                  disabled={!customerNameInput.trim() || isLoading}
                  loading={isLoading}
                >
                  Next →
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}

        {/* Step 2: Setup Instructions */}
        {step === 2 && (
          <Container>
            <SpaceBetween size="m">
              <Box variant="h3">Setup Instructions for {customerName}</Box>
              
              <Alert type="info">
                Share these instructions with your customer to set up cross-account access.
              </Alert>

              <Box>
                <Box variant="h4">Setup Details:</Box>
                <Box margin={{ top: 's' }}>
                  <Box>• <strong>Role Name:</strong> {roleName}</Box>
                  <Box>• <strong>External ID:</strong> {externalId}</Box>
                  <Box>• <strong>MSP Account:</strong> {mspAccountId || '(Loading...)'}</Box>
                  <Box>• <strong>MSP Principal:</strong> <code style={{ fontSize: '11px' }}>{mspPrincipalArn || '(Loading...)'}</code></Box>
                </Box>
              </Box>

              <ExpandableSection headerText="AWS CLI Commands" defaultExpanded>
                <SpaceBetween size="m">
                  <Alert type="success">
                    Commands are pre-populated with your MSP principal ARN. Copy and run in the customer's AWS account.
                  </Alert>

                  {/* Step 1: Create Role */}
                  <Box>
                    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                      <Box variant="h5">Step 1: Create the IAM role with trust policy</Box>
                      <CopyButton text={createRoleCommand} label="Copy create-role command" />
                    </SpaceBetween>
                    <Box padding="s">
                      <pre style={{ 
                        fontSize: '11px', 
                        margin: 0, 
                        whiteSpace: 'pre-wrap', 
                        wordBreak: 'break-all', 
                        backgroundColor: '#f5f5f5', 
                        padding: '12px', 
                        borderRadius: '4px',
                        maxHeight: '200px',
                        overflow: 'auto'
                      }}>
                        {createRoleCommand}
                      </pre>
                    </Box>
                  </Box>

                  {/* Step 2: Attach Policy */}
                  <Box>
                    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                      <Box variant="h5">Step 2: Attach the MSP permissions policy</Box>
                      <CopyButton text={attachPolicyCommand} label="Copy put-role-policy command" />
                    </SpaceBetween>
                    <Box padding="s">
                      <pre style={{ 
                        fontSize: '11px', 
                        margin: 0, 
                        whiteSpace: 'pre-wrap', 
                        wordBreak: 'break-all', 
                        backgroundColor: '#f5f5f5', 
                        padding: '12px', 
                        borderRadius: '4px',
                        maxHeight: '300px',
                        overflow: 'auto'
                      }}>
                        {attachPolicyCommand}
                      </pre>
                    </Box>
                  </Box>

                  {/* Step 3: Verify */}
                  <Box>
                    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                      <Box variant="h5">Step 3: Verify role creation</Box>
                      <CopyButton text={verifyRoleCommand} label="Copy get-role command" />
                    </SpaceBetween>
                    <Box padding="s">
                      <pre style={{ 
                        fontSize: '11px', 
                        margin: 0, 
                        whiteSpace: 'pre-wrap', 
                        backgroundColor: '#f5f5f5', 
                        padding: '12px', 
                        borderRadius: '4px'
                      }}>
                        {verifyRoleCommand}
                      </pre>
                    </Box>
                  </Box>
                </SpaceBetween>
              </ExpandableSection>

              <ExpandableSection headerText="Required Permissions by Agent">
                <SpaceBetween size="s">
                  <Box>
                    <Box variant="h5">CloudWatch Agent</Box>
                    <Box>• <code>cloudwatch:Describe/Get/List*</code> — alarms, metrics, dashboards</Box>
                    <Box>• <code>logs:DescribeLogGroups/Streams/QueryDefinitions</code></Box>
                    <Box>• <code>logs:StartQuery, GetQueryResults, FilterLogEvents</code></Box>
                    <Box>• <code>logs:GetLogEvents, GetLogRecord</code></Box>
                    <Box>• <code>ec2:DescribeInstances, DescribeInstanceStatus</code> — health checks</Box>
                  </Box>
                  <Box>
                    <Box variant="h5">Security Hub Agent</Box>
                    <Box>• <code>securityhub:GetFindings, ListFindings</code></Box>
                    <Box>• <code>securityhub:GetEnabledStandards, DescribeStandards</code></Box>
                    <Box>• <code>securityhub:ListSecurityControlDefinitions</code></Box>
                    <Box>• <code>securityhub:BatchUpdateFindings</code> — for remediation</Box>
                  </Box>
                  <Box>
                    <Box variant="h5">Cost Explorer Agent</Box>
                    <Box>• <code>ce:GetCostAndUsage, GetCostForecast</code></Box>
                    <Box>• <code>ce:GetRightsizingRecommendation, GetAnomalies</code></Box>
                    <Box>• <code>ce:GetReservation/SavingsPlans*</code></Box>
                  </Box>
                  <Box>
                    <Box variant="h5">Trusted Advisor Agent</Box>
                    <Box>• <code>support:DescribeTrustedAdvisorChecks</code></Box>
                    <Box>• <code>support:DescribeTrustedAdvisorCheckResult/Summaries</code></Box>
                    <Box>• <em>Note: Requires AWS Business/Enterprise support plan</em></Box>
                  </Box>
                  <Box>
                    <Box variant="h5">General (all agents)</Box>
                    <Box>• <code>sts:GetCallerIdentity</code> — credential verification</Box>
                    <Box>• <code>tag:GetResources</code> — resource discovery</Box>
                    <Box>• <code>apigateway:*</code> — remediation actions</Box>
                  </Box>
                </SpaceBetween>
              </ExpandableSection>

              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setStep(1)}>← Back</Button>
                <Button variant="primary" onClick={() => setStep(3)}>
                  Next: Test Connection →
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}

        {/* Step 3: Test Connection */}
        {step === 3 && (
          <Container>
            <SpaceBetween size="m">
              <Box variant="h3">Test Connection - {customerName}</Box>
              {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
              <FormField 
                label="Customer AWS Account ID"
                description="12-digit AWS account ID from customer"
              >
                <Input
                  value={accountId}
                  onChange={({ detail }) => setAccountId(detail.value)}
                  placeholder="123456789012"
                  inputMode="numeric"
                />
              </FormField>

              <FormField 
                label="Description (Optional)"
                description="Internal notes about this customer"
              >
                <Textarea
                  value={description}
                  onChange={({ detail }) => setDescription(detail.value)}
                  placeholder="Brief description of the customer account"
                  rows={3}
                />
              </FormField>

              {accountId && accountId.length === 12 && (
                <Alert type="success">
                  Account ID format is valid
                </Alert>
              )}

              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setStep(2)}>← Back</Button>
                <Button 
                  variant="primary" 
                  onClick={handleStep3Complete}
                  disabled={!accountId || accountId.length !== 12 || isLoading}
                  loading={isLoading}
                >
                  Complete Setup
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}
      </SpaceBetween>
    </Modal>
  );
}
