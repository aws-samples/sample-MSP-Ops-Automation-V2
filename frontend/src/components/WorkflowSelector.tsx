// frontend/src/components/WorkflowSelector.tsx
/**
 * Workflow mode selector.
 *
 * Renders two Cloudscape `Toggle` controls backed by `workflowStore`:
 *
 * - **Smart Workflows** — master toggle. When enabled, the backend will
 *   attempt to trigger automated CloudWatch → Jira → Knowledge Base →
 *   Remediation workflows in response to qualifying chat messages.
 *
 * - **Full Automation Mode** — only visible when Smart Workflows is on.
 *   When enabled, every workflow step executes without pausing for user
 *   approval. When off, the user is prompted to approve each step
 *   individually (manual approval mode).
 *
 * An `Alert` below the second toggle reflects the current automation level
 * so it is always visible without expanding the workflow panel.
 */

import React from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Toggle from '@cloudscape-design/components/toggle';
import Alert from '@cloudscape-design/components/alert';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { useWorkflowStore } from '../store/workflowStore';

export function WorkflowSelector() {
  const { 
    smartWorkflowsEnabled, 
    fullAutomationEnabled,
    setSmartWorkflows,
    setFullAutomation 
  } = useWorkflowStore();

  return (
    <Container header={<Header variant="h3">Workflow mode</Header>}>
      <SpaceBetween size="m">
        <Toggle
          checked={smartWorkflowsEnabled}
          onChange={({ detail }) => setSmartWorkflows(detail.checked)}
          description="Automate CloudWatch to Jira to Knowledge Base to Remediation workflow"
        >
          Enable Smart Workflows
        </Toggle>

        {smartWorkflowsEnabled && (
          <>
            <Toggle
              checked={fullAutomationEnabled}
              onChange={({ detail }) => setFullAutomation(detail.checked)}
              description="Execute all workflow steps automatically without manual approval"
            >
              Full Automation Mode
            </Toggle>

            {fullAutomationEnabled ? (
              <Alert type="success">
                Full automation enabled. All workflow steps will execute automatically.
              </Alert>
            ) : (
              <Alert type="info" header="Manual approval mode">
                You'll be prompted to approve each workflow step.
              </Alert>
            )}
          </>
        )}
      </SpaceBetween>
    </Container>
  );
}
