// frontend/src/components/WorkflowProgress.tsx
/**
 * Live progress display for full-automation workflow runs.
 *
 * Reads `fullAutoStatus` from `workflowStore`, which is updated incrementally by
 * `ApprovalCards` as each `workflow_step` event arrives from the backend poll.
 * Steps are rendered as soon as they appear in the store, so the list grows in real
 * time from the first `executing` event to the final `completed`/`failed` event.
 *
 * Progress bar logic:
 * - While `running === true`: percentage = completed steps / TOTAL_STEPS (0–100).
 * - Once `running === false` and steps exist: snaps to 100 % to signal completion.
 * - The `status` prop on `ProgressBar` switches to `"error"` if any step failed.
 *
 * The `onComplete` callback (used by `WorkflowPanel` to close the panel) is invoked
 * 2 s after `running` flips to false, giving the user a moment to read the final state
 * before the panel dismisses itself.
 *
 * Upcoming steps that have not yet received an event are shown as pending `StatusIndicator`
 * rows so the user always sees the full five-step roadmap from the start.
 */

import React, { useEffect } from 'react';
import Alert from '@cloudscape-design/components/alert';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { useWorkflowStore, WORKFLOW_STEP_NAMES } from '../store/workflowStore';

const TOTAL_STEPS = WORKFLOW_STEP_NAMES.length;

/**
 * @property workflowId - ID of the active workflow (currently unused in rendering, reserved for future API calls).
 * @property onComplete - Optional callback fired 2 s after the run finishes so the parent can close the panel.
 */
interface WorkflowProgressProps {
  workflowId?: string;
  onComplete?: () => void;
}

export function WorkflowProgress({ onComplete }: WorkflowProgressProps) {
  const { fullAutoStatus } = useWorkflowStore();
  const { running, steps } = fullAutoStatus;

  // Call onComplete 2s after steps arrive so the user sees the final state
  useEffect(() => {
    if (!running && steps.length > 0 && onComplete) {
      const timer = setTimeout(onComplete, 2000);
      return () => clearTimeout(timer);
    }
  }, [running, steps.length, onComplete]);

  const completedCount = steps.filter((s) => s.status === 'completed' || s.status === 'failed').length;
  // Snap to 100% once running stops to avoid a partial bar on the completion screen.
  const progressPercent = running
    ? Math.round((completedCount / TOTAL_STEPS) * 100)
    : completedCount > 0 ? 100 : 0;

  const allFailed = steps.length > 0 && steps.every((s) => s.status === 'failed');
  const anyFailed = steps.some((s) => s.status === 'failed');
  const alertType = allFailed ? 'error' : !running && completedCount > 0 ? 'success' : 'info';
  const header = running
    ? 'Full automation in progress'
    : completedCount > 0
      ? 'Full automation completed'
      : 'Full automation starting...';

  return (
    <Alert type={alertType} header={header}>
      <SpaceBetween size="m">
        <ProgressBar
          label={running ? `Step ${completedCount + 1} of ${TOTAL_STEPS}` : `${completedCount} of ${TOTAL_STEPS} steps`}
          description="Automating incident response workflow"
          value={progressPercent}
          status={anyFailed ? 'error' : !running && completedCount > 0 ? 'success' : 'in-progress'}
        />

        <SpaceBetween size="s">
          {steps.map((step, index) => {
            const statusType = step.status === 'failed' ? 'error'
              : step.status === 'executing' ? 'in-progress'
              : 'success';
            return (
              <StatusIndicator key={index} type={statusType}>
                Step {step.step_num}/{TOTAL_STEPS}: {step.step_name || WORKFLOW_STEP_NAMES[index]} — {step.status.toUpperCase()}
              </StatusIndicator>
            );
          })}

          {/* Show the next step as "waiting" only when no step is actively executing,
              so there is always a visual hint of what comes next without doubling up
              when a step transitions from executing to completed. */}
          {running && steps.length < TOTAL_STEPS && !steps.some((s) => s.status === 'executing') && (
            <StatusIndicator type="pending">
              Step {steps.length + 1}/{TOTAL_STEPS}: {WORKFLOW_STEP_NAMES[steps.length] ?? 'Processing'} — waiting
            </StatusIndicator>
          )}

          {/* Render all remaining future steps as pending so the full roadmap is always visible. */}
          {running && Array.from({ length: Math.max(0, TOTAL_STEPS - steps.length - 1) }, (_, i) => (
            <StatusIndicator key={i} type="pending">
              Step {steps.length + i + 2}/{TOTAL_STEPS}: {WORKFLOW_STEP_NAMES[steps.length + i + 1] ?? `Step ${steps.length + i + 2}`}
            </StatusIndicator>
          ))}
        </SpaceBetween>
      </SpaceBetween>
    </Alert>
  );
}
