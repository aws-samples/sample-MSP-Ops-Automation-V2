// frontend/src/components/ApprovalCards.tsx
/**
 * Renders pending workflow approval gates and handles approve/reject actions.
 *
 * Supports two execution modes:
 * - **Step-by-step**: each step (`jira`, `kb_search`, `remediation`, `verification`,
 *   `closure`) surfaces its own approval card; the user approves or skips one at a time.
 * - **Full automation** (`full_auto`): a single card gates the entire run; on approval
 *   the backend executes all steps sequentially, emitting incremental `workflow_step`
 *   events that are forwarded to `fullAutoStatus` in `workflowStore` for live display
 *   in `WorkflowProgress`.
 *
 * Polling is active only while `pendingWorkflowId` is set in the store.  An exponential
 * backoff caps poll frequency when no approvals are pending, and polling stops entirely
 * after `MAX_EMPTY_POLLS` consecutive empty responses to avoid runaway requests after a
 * workflow has silently completed or been abandoned.
 */

import React, { useState, useEffect } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import { apiClient } from '../services/api/apiClient';
import { useChatStore } from '../store/chatStore';
import { useWorkflowStore, WORKFLOW_STEP_NAMES, type FullAutoStep, type FullAutoStatus } from '../store/workflowStore';
import type { Approval } from '../types';

/**
 * Props for `ApprovalCards`.
 *
 * @property onApprovalProcessed - Callback fired after any approve or reject action
 *   completes successfully.  Used by the parent to perform follow-up work (e.g. logging).
 */
interface ApprovalCardProps {
  onApprovalProcessed: () => void;
}

export function ApprovalCards({ onApprovalProcessed }: ApprovalCardProps) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [showingResult, setShowingResult] = useState(false);

  const { pendingWorkflowId, setPendingWorkflowId } = useWorkflowStore();

  // Poll only while a workflow is actively pending — avoids 30 req/min when idle.
  // Uses exponential backoff: 2s → 4s → 8s after 5 consecutive empty responses,
  // resets to 2s whenever a new approval arrives.
  // Stops entirely after 15 consecutive empty polls (~60s) to prevent runaway polling
  // after the workflow has already completed or been abandoned.
  const MAX_EMPTY_POLLS = 15;

  useEffect(() => {
    if (!pendingWorkflowId || showingResult) return;

    let timeoutId: ReturnType<typeof setTimeout>;
    let emptyCount = 0;

    const fetchApprovals = async () => {
      try {
        const result = await apiClient.getPendingWorkflows();
        if (result.success) {
          const incoming: Approval[] = result.approvals || [];
          if (incoming.length > 0) {
            emptyCount = 0; // reset backoff when approvals are present
          } else {
            emptyCount++;
          }
          setApprovals(incoming);
          if (incoming.length === 0 && emptyCount >= MAX_EMPTY_POLLS) {
            // Workflow has been idle for ~60s — stop polling
            setPendingWorkflowId(null);
            return;
          }
        }
      } catch (error) {
        console.error('Failed to fetch pending approvals:', error);
        emptyCount++;
        if (emptyCount >= MAX_EMPTY_POLLS) {
          setPendingWorkflowId(null);
          return;
        }
      }
      // Backoff: 2s base, double after 5 empty polls, cap at 8s
      const delay = emptyCount >= 5 ? Math.min(2000 * Math.pow(2, Math.floor(emptyCount / 5)), 8000) : 2000;
      timeoutId = setTimeout(fetchApprovals, delay);
    };

    fetchApprovals();
    return () => clearTimeout(timeoutId);
  }, [pendingWorkflowId, showingResult, setPendingWorkflowId]);

  const addMessage = useChatStore((state) => state.addMessage);
  const setFullAutoStatus = useWorkflowStore((state) => state.setFullAutoStatus);

  const handleApprove = async (workflowId: string, stepType: string) => {
    setIsLoading(true);

    // For full automation, show immediate start message BEFORE polling
    // full_auto branching: kick off WorkflowProgress display and post a start message
    // immediately so the UI is responsive before the long-running poll begins.
    if (stepType === 'full_auto') {
      setFullAutoStatus({ running: true, steps: [] });
      addMessage({
        id: `workflow-${workflowId}-auto-start-${Date.now()}`,
        content: '**Full Automation Started**\n\nExecuting all workflow steps automatically. This may take several minutes.\n\nYou\'ll see each step result as it completes.',
        sender: 'agent',
        timestamp: new Date(),
        workflowId: workflowId,
        workflowStep: 'Full Automation'
      });
    }
    
    try {
      // For full_auto, pass an incremental progress callback
      const onStepProgress = stepType === 'full_auto' ? (stepData: FullAutoStep) => {
        // Update store: upsert step by step_num (handles executing -> completed transitions)
        setFullAutoStatus((prev: FullAutoStatus) => {
          const existingIdx = prev.steps.findIndex(
            (s: FullAutoStep) => s.step_num === stepData.step_num
          );

          // No-op if status unchanged — same reference prevents Zustand re-render
          if (existingIdx >= 0 && prev.steps[existingIdx].status === stepData.status) {
            return prev;
          }

          const stepEntry: FullAutoStep = {
            step_num: stepData.step_num,
            step_name: stepData.step_name,
            status: stepData.status,
            result: stepData.result,
            message: stepData.message,
          };

          let newSteps;
          if (existingIdx >= 0) {
            // Update existing step (executing -> completed)
            newSteps = [...prev.steps];
            newSteps[existingIdx] = stepEntry;
          } else {
            newSteps = [...prev.steps, stepEntry];
          }
          return { running: true, steps: newSteps };
        });

        // Only add chat message for terminal states
        if (stepData.status === 'completed' || stepData.status === 'failed') {
          const statusText = stepData.status === 'completed' ? 'Completed' : 'Failed';
          let stepContent = stepData.result || stepData.message || '';

          // For remediation steps, check if result contains CLI commands and format them
          if (stepData.step_name?.toLowerCase().includes('remediation') && stepContent.includes('`')) {
            // Result already has markdown formatting from backend, pass through
          }

          addMessage({
            id: `workflow-${workflowId}-step-${stepData.step_num}-${Date.now()}`,
            content: `**Step ${stepData.step_num}/${WORKFLOW_STEP_NAMES.length}: ${stepData.step_name}**\n\n${statusText}\n\n${stepContent}`,
            sender: 'agent',
            timestamp: new Date(),
            workflowId: workflowId,
            workflowStep: stepData.step_name,
          });
        }
      } : undefined;

      const result = await apiClient.approveWorkflowStep(workflowId, stepType, onStepProgress);

      console.log(`Approved ${stepType}:`, result); // nosemgrep: unsafe-formatstring

      // Handle full automation mode completion
      if (stepType === 'full_auto') {
          setFullAutoStatus({ running: false, steps: result.step_results || [] });

          // Final summary message
          addMessage({
            id: `workflow-${workflowId}-complete-${Date.now()}`,
            content: result.result || '**Full Automation Complete**\n\nAll workflow steps have been executed.',
            sender: 'agent',
            timestamp: new Date(),
            workflowId: workflowId,
            workflowStep: 'Complete'
          });

      } else {
        // Standard single-step approval - ALWAYS add result to chat with formatting
        // Display result whether success or failure
        const stepName = result.step_name || stepType;
        const statusText = result.success ? 'Completed' : 'Warning';

        // Format execution_log as code blocks for remediation step
        let displayContent: string;
        if (result.execution_log?.length > 0) {
          const logEntries = result.execution_log.map((log: { step?: { cli_command?: string }; status?: string; error?: string }) => {
            const cmd = log.step?.cli_command || 'unknown';
            const logStatus = (log.status || 'UNKNOWN').toUpperCase();
            const err = log.status === 'failed' && log.error ? `\nError: ${log.error}` : '';
            return `**${cmd.split(' ').slice(0, 3).join(' ')}...**: ${logStatus}\n\`\`\`bash\n${cmd}\n\`\`\`${err}`;
          });
          displayContent = logEntries.join('\n\n');
        } else {
          displayContent = result.result || result.error || result.content || `${stepName} completed.`;
        }

        addMessage({
          id: `workflow-${workflowId}-${stepType}-${Date.now()}`,
          content: `**${stepName}** — ${statusText}\n\n${displayContent}`,
          sender: 'agent',
          timestamp: new Date(),
          workflowId: workflowId,
          workflowStep: stepName
        });
      }
      
      onApprovalProcessed();
      
      // Briefly hide the approval panel so the user sees the step result appear in chat
      // before the next approval card renders.  Mimics the Streamlit sequential-reveal UX.
      // The polling useEffect is gated on `showingResult` so it pauses during this window,
      // then resumes naturally and fetches the next approval when the timeout fires.
      setShowingResult(true);
      setApprovals([]);

      // 3 s delay gives the user time to read the result before the next gate appears.
      setTimeout(() => {
        setShowingResult(false);
      }, 3000);
    } catch (error) {
      console.error(`Approval failed for ${stepType}:`, error); // nosemgrep: unsafe-formatstring
    } finally {
      setIsLoading(false);
    }
  };

  const handleReject = async (workflowId: string, stepType: string) => {
    setIsLoading(true);
    try {
      const result = await apiClient.rejectWorkflowStep(workflowId, stepType);
      
      if (result.success) {
        console.log(`Rejected ${stepType}`);
        onApprovalProcessed();
        
        // Refresh approvals list
        const pendingResult = await apiClient.getPendingWorkflows();
        setApprovals(pendingResult.approvals || []);
      }
    } catch (error) {
      console.error(`Rejection failed for ${stepType}:`, error); // nosemgrep: unsafe-formatstring
    } finally {
      setIsLoading(false);
    }
  };

  const getApprovalConfig = (type: string, approval?: Approval) => {
    switch (type) {
      case 'full_auto':
        return {
          header: 'Full Automation',
          description: approval?.disclaimer || 'This will automatically execute all remediation steps: Jira ticket creation, knowledge base search, remediation, and ticket closure.',
          approveText: 'Start Automation',
          rejectText: 'Cancel',
          alertType: 'warning' as const,
          isFullAuto: true
        };
      case 'jira':
        return {
          header: 'Alarm detection',
          description: '2 alarm(s) detected and require attention.',
          approveText: 'Create ticket',
          rejectText: 'Skip',
          alertType: 'warning' as const
        };
      case 'kb_search':
        return {
          header: 'Knowledge base search',
          description: 'Jira ticket created. Search knowledge base for troubleshooting steps?',
          approveText: 'Search KB',
          rejectText: 'Skip KB',
          alertType: 'info' as const
        };
      case 'remediation':
        return {
          header: 'Remediation execution',
          description: 'Knowledge base guidance provided. Execute automated remediation?',
          approveText: 'Remediate',
          rejectText: 'Skip remediation',
          alertType: 'info' as const
        };
      case 'verification':
        return {
          header: 'Alarm verification',
          description: 'Remediation executed. Verify the alarm has returned to OK state?',
          approveText: 'Verify alarm',
          rejectText: 'Skip verification',
          alertType: 'info' as const
        };
      case 'closure':
        return {
          header: 'Ticket closure',
          description: 'Verification complete. Close the Jira ticket?',
          approveText: 'Close ticket',
          rejectText: 'Keep open',
          alertType: 'info' as const
        };
      default:
        return {
          header: 'Pending approval',
          description: 'Action required',
          approveText: 'Approve',
          rejectText: 'Reject',
          alertType: 'info' as const
        };
    }
  };

  if (!approvals || approvals.length === 0) {
    return null;
  }

  return (
    <Container header={<Header variant="h3">Pending approvals</Header>}>
      <SpaceBetween size="m">
        {(approvals ?? []).map((approval) => {
          const config = getApprovalConfig(approval.type, approval);
          
          return (
            <Alert
              key={approval.workflow_id}
              type={config.alertType}
              header={config.header}
              action={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button 
                    variant="primary"
                    onClick={() => handleApprove(approval.workflow_id, approval.type)}
                    loading={isLoading}
                  >
                    {config.approveText}
                  </Button>
                  <Button 
                    onClick={() => handleReject(approval.workflow_id, approval.type)}
                    disabled={isLoading}
                  >
                    {config.rejectText}
                  </Button>
                </SpaceBetween>
              }
            >
              <SpaceBetween size="s">
                {config.description}
                
              </SpaceBetween>
            </Alert>
          );
        })}
      </SpaceBetween>
    </Container>
  );
}
