// frontend/src/types/index.ts
/**
 * TypeScript type definitions for MSP Assistant.
 *
 * Organised into logical groups:
 * - Auth (`User`, `AuthTokens`)
 * - Account management (`Account`, `Approval`)
 * - SSE / streaming (`SSEProgressEvent`, `StreamingState`, `ThinkingStep`, `RoutingDecision`)
 * - Chat (`ChatMessage`, `ChatRequest`, `ChatResponse`, `ChatPollResponse`)
 * - Workflow state (`WorkflowState`, `WorkflowStep`, `WorkflowPanelState`, `WorkflowPanelControl`)
 * - Health / ops (`AlarmInfo`, `HealthEvent`)
 */

/** Authenticated Cognito user identity. */
export interface User {
  userId: string;
  email: string;
  username: string;
}

/** Cognito token bundle held in memory (never written to localStorage). */
export interface AuthTokens {
  idToken: string;
  accessToken: string;
  refreshToken: string;
}

/** An AWS account registered with the MSP platform. */
export interface Account {
  /** Frontend-only stable key; `"default"` for the MSP account, otherwise matches `account_id`. */
  id: string;
  name: string;
  /** `"msp"` for the operator's own account; `"customer"` for onboarded managed accounts. */
  type: 'msp' | 'customer';
  /**
   * Reflects the STS assume-role health of the account.
   * `"pending"` — role created but not yet assumed; `"expired"` — STS token stale;
   * `"error"` — assume-role failed (see `sts_error` / `error` for details).
   */
  status: 'active' | 'inactive' | 'pending' | 'expired' | 'error';
  /** AWS account ID (12-digit string), absent for the default MSP account. */
  account_id?: string;
  /** IAM cross-account role name created in the customer account. */
  role_name?: string;
  /** ExternalId used in the IAM role trust policy to prevent confused-deputy attacks. */
  external_id?: string;
  created_at?: string;
  /** True when the STS token is approaching expiry and should be refreshed. */
  needs_refresh?: boolean;
  /** STS error details for pending/failed accounts. */
  sts_error?: string;
  /** General error details (e.g. missing role, wrong external ID). */
  error?: string;
}

/** A pending human-approval gate in an active workflow. */
export interface Approval {
  workflow_id: string;
  /**
   * Identifies which step requires approval.
   * `"full_auto"` — single gate to run all steps; remaining types are individual step gates.
   */
  type: 'full_auto' | 'jira' | 'kb_search' | 'remediation' | 'closure';
  /** Warning text shown to the user before they approve (e.g. for full_auto). */
  disclaimer?: string;
  description?: string;
}

/**
 * A single server-sent event emitted by the backend streaming endpoint.
 *
 * Event lifecycle: `progress`* → `agent_switch`? → (`tool_call` + `tool_result`)* →
 * `content`* → `complete` | `error`.
 *
 * @property event - Discriminant for the event type.
 * @property data.stage - Processing stage label (on `progress` events).
 * @property data.to_agent - Target specialist agent name (on `agent_switch` events).
 * @property data.tool_name - Name of the invoked tool (on `tool_call`/`tool_result` events).
 * @property data.result - Full `ChatResponse` payload (on `complete` events only).
 * @property request_id - Echo of the originating request ID for correlation.
 */
export interface SSEProgressEvent {
  event: 'progress' | 'agent_switch' | 'tool_call' | 'tool_result' | 'content' | 'complete' | 'error';
  data: {
    stage?: string;
    message?: string;
    agent_type?: string;
    from_agent?: string;
    to_agent?: string;
    tool_name?: string;
    status?: string;
    text?: string;
    content?: string;
    result?: ChatResponse;
    error?: string;
  };
  request_id?: string;
}

/** Transient UI state mirroring the live SSE stream (subset of `chatStore`). */
export interface StreamingState {
  isStreaming: boolean;
  streamingContent: string;
  streamingAgent: string;
  streamingStage: string;
}

/** A single step in the agent's thinking/reasoning process (captured from SSE events) */
export interface ThinkingStep {
  id: string;                   // Unique step ID
  type: 'progress' | 'agent_switch' | 'tool_call' | 'tool_result' | 'content';
  message: string;              // Human-readable description
  agentName?: string;           // Agent that owns this step (e.g., "cloudwatch")
  toolName?: string;            // Tool name if type is 'tool_call'
  routingReason?: string;       // The prompt passed to the specialist (WHY routing happened)
  timestamp: number;            // Date.now() when captured
  status: 'success' | 'in-progress' | 'pending' | 'error';
}

/** Supervisor routing metadata extracted from SSE events */
export interface RoutingDecision {
  queryType: string;            // e.g., "CloudWatch alarm status check"
  selectedAgent: string;        // e.g., "cloudwatch"
  alternativeAgents: string;    // e.g., "security, cost" (comma-separated)
  routingReason?: string;       // The actual prompt sent to the specialist
  timestamp: number;
}

/** A single message in the conversation history (user turn or agent response). */
export interface ChatMessage {
  id: string;
  content: string;
  sender: 'user' | 'agent';
  /** Specialist agent that produced this response (e.g. `"cloudwatch"`, `"security"`). */
  agentType?: string;
  timestamp: Date;
  /** Wall-clock time from request submission to first complete response, in milliseconds. */
  responseTimeMs?: number;
  workflowTriggered?: boolean;
  workflowId?: string;
  /** Workflow step name used as a label when displaying step results in chat. */
  workflowStep?: string;
  /** True when this message is a gate waiting for human approval (renders `ApprovalCards`). */
  requiresApproval?: boolean;
  /** True when the workflow is running in full-automation mode (no per-step gates). */
  isAutomated?: boolean;
  /** Thinking steps captured from SSE events and attached to this message after completion. */
  thinkingSteps?: ThinkingStep[];
  /** Supervisor routing metadata attached after the `agent_switch` SSE event. */
  routingDecision?: RoutingDecision;
}

/** Payload sent to `POST /chat` to initiate a new assistant request. */
export interface ChatRequest {
  message: string;
  /** Target account name; omit to use the MSP's own AWS account context. */
  account_name?: string;
  /** When true, the backend runs the Smart Workflows graph instead of a plain chat response. */
  workflow_enabled?: boolean;
  /** When true AND `workflow_enabled`, skips per-step gates and runs all steps automatically. */
  full_automation?: boolean;
  /** AgentCore conversation session ID for STM scoping. */
  conversation_id?: string;
}

/** Final response returned when a chat request completes successfully. */
export interface ChatResponse {
  success: boolean;
  content: string;
  /** Specialist agent that handled the request (used to label the response badge). */
  agent_type: string;
  /** True when the backend created a new workflow instance for this request. */
  workflow_triggered?: boolean;
  workflow_id?: string | null;
}

/** Shape of `GET /chat/:id` poll responses while a request is in-flight. */
export interface ChatPollResponse {
  request_id: string;
  status: 'processing' | 'complete' | 'error';
  progress: {
    /** Current pipeline stage; drives the streaming indicator label. */
    stage: 'received' | 'routing' | 'delegating' | 'waiting' | 'complete' | 'error';
    /** Optional early hint at which agent will handle the request (before routing completes). */
    agent_hint?: string;
    message: string;
    elapsed_seconds: number;
  };
  /** Populated only when `status === "complete"`. */
  result?: ChatResponse;
}

/** Feature-flag configuration sent with every chat request to the backend. */
export interface WorkflowState {
  /** Master toggle for Smart Workflows (alarm-triggered automation). */
  smartWorkflowsEnabled: boolean;
  /** When enabled, a single gate approves all steps (no per-step confirmation). */
  fullAutomationEnabled: boolean;
  /** When true, remediation steps are sourced from the knowledge base rather than a static runbook. */
  useDynamicRemediation: boolean;
}

/** CloudWatch alarm metadata surfaced in the health dashboard. */
export interface AlarmInfo {
  name: string;
  threshold: string;
  current: string;
  /** How long the alarm has been in the breaching state. */
  duration: string;
}

/** A single AWS Health event shown in the health dashboard. */
export interface HealthEvent {
  service: string;
  region: string;
  status: 'warning' | 'info' | 'pending' | 'error';
  message: string;
}

/** A single step row in a workflow progress display. */
export interface WorkflowStep {
  label: string;
  status: 'success' | 'in-progress' | 'pending' | 'error';
  /** Optional descriptive text or result from this step. */
  text?: string;
}

/** Visibility state for the `WorkflowPanel` side panel. */
export interface WorkflowPanelState {
  isOpen: boolean;
  activeWorkflowId: string | null;
  /** When true, `WorkflowProgress` is rendered (full-automation mode). */
  showProgress: boolean;
  /** When true, `ApprovalCards` is rendered (step-by-step mode). */
  showApprovals: boolean;
}

/** Actions exposed by the workflow panel controller. */
export interface WorkflowPanelControl {
  openPanel: (workflowId: string, showProgress: boolean) => void;
  closePanel: () => void;
  setActiveWorkflow: (workflowId: string | null) => void;
}
