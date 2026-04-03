// frontend/src/store/workflowStore.ts
/**
 * Workflow state machine for MSP incident-response automation.
 *
 * Manages three orthogonal concerns in a single Zustand store:
 *
 * 1. **Configuration flags** (`smartWorkflowsEnabled`, `fullAutomationEnabled`,
 *    `useDynamicRemediation`) — toggled by `WorkflowSelector` and sent with every
 *    chat request to tell the backend which mode to run.
 *
 * 2. **Panel visibility** (`isOpen`, `showProgress`, `showApprovals`) — drives
 *    whether `WorkflowPanel` renders `WorkflowProgress` or `ApprovalCards`.
 *
 * 3. **Live step state** (`fullAutoStatus`, `pendingWorkflowId`) — updated in real
 *    time by `ApprovalCards` as step events arrive; consumed by `WorkflowProgress`
 *    for the animated progress bar.
 *
 * State transitions:
 * - Chat response with `workflow_triggered === true` → `setPendingWorkflowId` starts
 *   approval polling; `openWorkflowPanel` opens the side panel.
 * - User approves `full_auto` → `setFullAutoStatus({ running: true, steps: [] })`
 *   kicks off progress display; each step event upserts into `steps`.
 * - All steps done → `setFullAutoStatus({ running: false, ... })` triggers
 *   `WorkflowProgress` to call `onComplete` after a 2 s delay.
 * - `closeWorkflowPanel` resets all panel and step state atomically.
 */

import { create } from 'zustand';
import type { WorkflowState, WorkflowPanelState } from '../types';

/** Canonical ordered step names for the 5-step workflow. Single source of truth
 *  shared by WorkflowProgress (display) and ApprovalCards (result labels). */
export const WORKFLOW_STEP_NAMES = [
  'Creating Jira ticket',
  'Searching knowledge base',
  'Executing remediation',
  'Verifying alarm state',
  'Closing Jira ticket',
] as const;

/**
 * Represents a single step in a full-automation workflow execution.
 * Steps are received incrementally via `streaming_events` on the poll endpoint
 * and upserted into `FullAutoStatus.steps` by `ApprovalCards`.
 */
export interface FullAutoStep {
  /** 1-based ordinal matching `WORKFLOW_STEP_NAMES` index (step_num - 1). */
  step_num: number;
  step_name: string;
  /** Lifecycle value: `"executing"` while running, `"completed"` or `"failed"` when done. */
  status: string;
  /** Human-readable output from the backend for this step (markdown). */
  result?: string;
  /** Short status note, typically shown when the step is still executing. */
  message?: string;
}

/**
 * Live status snapshot for a full-automation run.
 * Consumed by `WorkflowProgress` to render the animated progress bar and
 * per-step status indicators.
 */
export interface FullAutoStatus {
  /** True while the backend is still executing steps; false once all steps have settled. */
  running: boolean;
  /** Ordered list of steps received so far (may be a partial list while `running === true`). */
  steps: FullAutoStep[];
}

interface WorkflowStoreState extends WorkflowState, WorkflowPanelState {
  fullAutoStatus: FullAutoStatus;
  /** Set when a workflow is triggered — signals ApprovalCards to start polling. */
  pendingWorkflowId: string | null;
  setSmartWorkflows: (enabled: boolean) => void;
  setFullAutomation: (enabled: boolean) => void;
  setDynamicRemediation: (enabled: boolean) => void;
  openWorkflowPanel: (workflowId: string, isAutomated: boolean) => void;
  closeWorkflowPanel: () => void;
  setActiveWorkflow: (workflowId: string | null) => void;
  setFullAutoStatus: (statusOrFn: FullAutoStatus | ((prev: FullAutoStatus) => FullAutoStatus)) => void;
  setPendingWorkflowId: (id: string | null) => void;
}

export const useWorkflowStore = create<WorkflowStoreState>((set) => ({
  // Workflow configuration state
  smartWorkflowsEnabled: false,
  fullAutomationEnabled: false,
  useDynamicRemediation: false,

  // Workflow panel state (starts closed)
  isOpen: false,
  activeWorkflowId: null,
  showProgress: false,
  showApprovals: false,

  // Full automation progress (drives WorkflowProgress panel)
  fullAutoStatus: { running: false, steps: [] },

  // Drives ApprovalCards polling — null means idle, string means active workflow
  pendingWorkflowId: null,

  // Workflow configuration actions
  /** Enable or disable the Smart Workflows feature flag sent with chat requests. */
  setSmartWorkflows: (enabled) => set({ smartWorkflowsEnabled: enabled }),
  /** Toggle full-automation mode; when true, a single approval executes all steps. */
  setFullAutomation: (enabled) => set({ fullAutomationEnabled: enabled }),
  /** Toggle dynamic (KB-driven) vs. static remediation path. */
  setDynamicRemediation: (enabled) => set({ useDynamicRemediation: enabled }),

  // Workflow panel actions
  /**
   * Open the workflow side panel for the given workflow.
   * @param isAutomated - When true, shows `WorkflowProgress`; otherwise shows `ApprovalCards`.
   */
  openWorkflowPanel: (workflowId, isAutomated) => set({
    isOpen: true,
    activeWorkflowId: workflowId,
    showProgress: isAutomated,
    showApprovals: !isAutomated,
  }),

  /** Close the workflow panel and reset all step progress atomically. */
  closeWorkflowPanel: () => set({
    isOpen: false,
    activeWorkflowId: null,
    showProgress: false,
    showApprovals: false,
    fullAutoStatus: { running: false, steps: [] },
  }),

  /** Update the active workflow ID without toggling panel visibility. */
  setActiveWorkflow: (workflowId) => set({ activeWorkflowId: workflowId }),
  /**
   * Replace or functionally update `fullAutoStatus`.
   * Accepts either a new value or an updater function (same pattern as React's `setState`)
   * so callers can safely upsert individual steps without reading stale state.
   */
  setFullAutoStatus: (statusOrFn) => set((state) => ({
    fullAutoStatus: typeof statusOrFn === 'function' ? statusOrFn(state.fullAutoStatus) : statusOrFn
  })),
  /**
   * Set the workflow ID that `ApprovalCards` should poll for.
   * Pass `null` to stop polling (e.g. after the workflow completes or times out).
   */
  setPendingWorkflowId: (id) => set({ pendingWorkflowId: id }),
}));
