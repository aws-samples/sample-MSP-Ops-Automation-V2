// frontend/src/components/WorkflowPanel.tsx
/**
 * Right-side tools panel that surfaces workflow state during an active incident response.
 *
 * Renders one of two views depending on `workflowStore` flags:
 * - `showProgress === true` (full-automation mode): renders `WorkflowProgress`, which
 *   displays a live animated progress bar driven by `fullAutoStatus` step events.
 * - `showApprovals === true` (step-by-step mode): renders `ApprovalCards`, which polls
 *   for pending approval gates and lets the user approve or reject each step.
 *
 * Falls back to an empty state when `activeWorkflowId` is null (no workflow in progress).
 * The panel header title switches between "Workflow Automation" and "Workflow Approvals"
 * to reflect the current mode.  The close button calls `closeWorkflowPanel`, which
 * resets all panel and step state atomically in the store.
 */

import React from 'react';
import { Box, Button, Header, SpaceBetween } from '@cloudscape-design/components';
import { useWorkflowStore } from '../store/workflowStore';
import { ApprovalCards } from './ApprovalCards';
import { WorkflowProgress } from './WorkflowProgress';

const WorkflowPanel: React.FC = () => {
  const { 
    activeWorkflowId, 
    showProgress, 
    showApprovals,
    closeWorkflowPanel 
  } = useWorkflowStore();

  const renderHeader = () => (
    <Header
      variant="h2"
      actions={
        <Button
          variant="icon"
          iconName="close"
          onClick={closeWorkflowPanel}
          ariaLabel="Close workflow panel"
        />
      }
    >
      {showProgress ? 'Workflow Automation' : 'Workflow Approvals'}
    </Header>
  );

  const renderContent = () => {
    if (!activeWorkflowId) {
      return renderEmptyState();
    }

    if (showProgress) {
      return <WorkflowProgress workflowId={activeWorkflowId} onComplete={closeWorkflowPanel} />;
    }

    if (showApprovals) {
      return <ApprovalCards onApprovalProcessed={() => {}} />;
    }

    return renderEmptyState();
  };

  const renderEmptyState = () => (
    <Box textAlign="center" padding={{ vertical: 'xxl' }}>
      <Box variant="p" color="text-body-secondary">
        No active workflow
      </Box>
      <Box variant="small" color="text-body-secondary" margin={{ top: 's' }}>
        Workflows will appear here when triggered by CloudWatch alarms
      </Box>
    </Box>
  );

  return (
    <SpaceBetween size="m">
      {renderHeader()}
      {renderContent()}
    </SpaceBetween>
  );
};

export default WorkflowPanel;
