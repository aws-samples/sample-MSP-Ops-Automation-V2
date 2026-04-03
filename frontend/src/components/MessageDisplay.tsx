// frontend/src/components/MessageDisplay.tsx
/**
 * Chat message rendering module.
 *
 * Exports two components:
 *
 * **`MessageDisplay`** — renders a single completed `ChatMessage` using a
 * Cloudscape `ChatBubble`. User messages use an outgoing bubble; agent
 * messages use an incoming gen-AI bubble with:
 * - One or more agent-type `Badge` labels (resolved via `AGENT_DISPLAY`).
 * - A response-time indicator.
 * - A clickable workflow badge that re-opens the `WorkflowPanel`.
 * - A collapsed `ThinkingLine` showing how long the agent reasoned, expandable
 *   to the full step history and routing decision.
 *
 * **`StreamingIndicator`** — a live `ChatBubble` shown only while `isStreaming`
 * is true in `chatStore`. Displays the real-time thinking steps and partial
 * response text as SSE tokens arrive. Hidden automatically when streaming ends.
 *
 * **`ThinkingLine`** (internal) — a compact collapsible row that shows the
 * latest thinking step as a cycling italic summary. Expanding it reveals all
 * accumulated steps with `StatusIndicator` icons and a routing-decision
 * `KeyValuePairs` panel. Text transitions use a short CSS opacity fade to
 * avoid jarring updates during rapid SSE bursts.
 *
 * Agent display config (`AGENT_DISPLAY`) maps lowercase agent keys to their
 * human-readable label and badge color. Unknown agents fall back to a grey
 * badge using the raw agent string.
 */

import React, { useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import ChatBubble from '@cloudscape-design/chat-components/chat-bubble';
import Avatar from '@cloudscape-design/chat-components/avatar';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Spinner from '@cloudscape-design/components/spinner';
import { useWorkflowStore } from '../store/workflowStore';
import { useChatStore } from '../store/chatStore';
import type { ChatMessage, ThinkingStep, RoutingDecision } from '../types';

// ─── Agent display config ─────────────────────────────────────────────────────

const AGENT_DISPLAY: Record<string, {
  label: string;
  color: 'blue' | 'green' | 'red' | 'grey' | 'severity-low' | 'severity-medium' | 'severity-high' | 'severity-critical';
}> = {
  cloudwatch:  { label: 'CloudWatch',     color: 'blue' },
  security:    { label: 'Security Hub',   color: 'red' },
  cost:        { label: 'Cost Explorer',  color: 'green' },
  advisor:     { label: 'Trusted Advisor', color: 'green' },
  jira:        { label: 'Jira',           color: 'red' },
  knowledge:   { label: 'Knowledge',      color: 'blue' },
  supervisor:  { label: 'Supervisor',     color: 'grey' },
  error:       { label: 'Error',          color: 'red' },
};

function getAgentDisplay(agentType?: string) {
  if (!agentType) return null;
  const key = agentType.toLowerCase().replace(/[^a-z]/g, '');
  return AGENT_DISPLAY[key] || { label: agentType, color: 'grey' as const };
}

function getMultiAgentDisplays(agentType?: string) {
  if (!agentType) return [];
  return agentType.split(',')
    .map(a => a.trim())
    .filter(Boolean)
    .map(getAgentDisplay)
    .filter((d): d is NonNullable<typeof d> => d !== null);
}

// ─── Thinking step helpers ────────────────────────────────────────────────────

function stepToStatusType(step: ThinkingStep): 'success' | 'in-progress' | 'pending' | 'error' {
  return step.status;
}

// ─── ThinkingLine component ────────────────────────────────────────────────────
// Single compact cycling line showing the current agent thought.
// Expands to show full step history with markdown and routing decision.

interface ThinkingLineProps {
  steps: ThinkingStep[];
  routingDecision?: RoutingDecision | null;
  isStreaming?: boolean;
  streamingStage?: string;   // fallback text when no steps yet
  responseTimeMs?: number;   // for completed messages: shows "Thought for Xs"
}

function ThinkingLine({
  steps,
  routingDecision,
  isStreaming = false,
  streamingStage = '',
  responseTimeMs,
}: ThinkingLineProps) {
  const [expanded, setExpanded] = useState(false);
  const [textVisible, setTextVisible] = useState(true);

  // Derive the current cycling text
  const latestStep = steps[steps.length - 1];
  const currentText = isStreaming
    ? (latestStep?.message || streamingStage || 'Processing...')
    : (responseTimeMs !== undefined
        ? `Thought for ${(responseTimeMs / 1000).toFixed(1)}s`
        : (latestStep?.message || ''));

  // Fade transition when cycling text changes
  const prevTextRef = useRef(currentText);
  useEffect(() => {
    if (currentText !== prevTextRef.current) {
      setTextVisible(false);
      const t = setTimeout(() => setTextVisible(true), 60);
      prevTextRef.current = currentText;
      return () => clearTimeout(t);
    }
  }, [currentText]);

  // Build routing KV items from routingDecision or derive from steps
  const agentSwitchStep = steps.find(s => s.type === 'agent_switch');
  const derivedAgent = agentSwitchStep?.agentName;
  const derivedReason = agentSwitchStep?.routingReason || routingDecision?.routingReason || '';
  const otherAgents = steps
    .filter(s => s.type === 'agent_switch' && s.agentName !== derivedAgent)
    .map(s => s.agentName)
    .filter(Boolean)
    .join(', ');

  const effectiveAgent = routingDecision?.selectedAgent || derivedAgent || '';
  const effectiveReason = routingDecision?.routingReason || derivedReason;
  const effectiveQuery = routingDecision?.queryType || '';
  const effectiveAlternatives = routingDecision?.alternativeAgents || otherAgents || '';
  const showRouting = !!(effectiveAgent || effectiveReason || effectiveQuery);

  const kvItems = showRouting ? [
    ...(effectiveQuery ? [{ label: 'Query', value: effectiveQuery }] : []),
    ...(effectiveAgent ? [{
      label: 'Routed to',
      value: (
        <StatusIndicator type="success">
          {getAgentDisplay(effectiveAgent)?.label || effectiveAgent}
        </StatusIndicator>
      ),
    }] : []),
    ...(effectiveReason ? [{
      label: 'Why',
      value: <Box color="text-body-secondary" fontSize="body-s">"{effectiveReason}"</Box>,
    }] : []),
    ...(effectiveAlternatives ? [{ label: 'Also considered', value: effectiveAlternatives }] : []),
  ] : [];

  if (!currentText && steps.length === 0 && !showRouting && !isStreaming) return null;

  const handleToggle = () => setExpanded(e => !e);
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleToggle();
    }
  };

  return (
    <Box>
      {/* Single cycling line — always visible */}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={handleToggle}
        onKeyDown={handleKeyDown}
        style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: 'pointer', userSelect: 'none' }}
      >
        {isStreaming ? (
          <Spinner size="normal" />
        ) : (
          <Box color="text-body-secondary" fontSize="body-s" display="inline">●</Box>
        )}
        <span
          style={{
            flex: 1,
            minWidth: 0,
            overflow: 'hidden',
            whiteSpace: 'nowrap',
            textOverflow: 'ellipsis',
            fontStyle: 'italic',
            fontSize: '12px',
            color: 'var(--color-text-body-secondary, #5f6b7a)',
            opacity: textVisible ? 1 : 0,
            transition: 'opacity 0.2s ease',
          }}
        >
          {currentText}
        </span>
        <Box color="text-body-secondary" fontSize="body-s" display="inline">
          {expanded ? '▲' : '▼'}
        </Box>
      </div>

      {/* Expanded detail — full step list + routing decision */}
      {expanded && (
        <Box margin={{ top: 'xs', left: 'l' }}>
          <SpaceBetween size="xs">
            {steps.map((step) => (
              <div key={step.id} style={{ display: 'flex', gap: '8px', alignItems: 'flex-start' }}>
                <StatusIndicator type={stepToStatusType(step)}>{''}</StatusIndicator>
                <Box color="text-body-secondary" fontSize="body-s">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{ p: ({ children }) => <span>{children}</span> }}
                  >
                    {step.message}
                  </ReactMarkdown>
                </Box>
              </div>
            ))}

            {/* Live spinner row at bottom when streaming */}
            {isStreaming && (
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <Spinner size="normal" />
                <Box color="text-status-info" fontSize="body-s">Processing...</Box>
              </div>
            )}

            {/* Routing decision */}
            {kvItems.length > 0 && (
              <Box margin={{ top: 'xs' }}>
                <Box color="text-body-secondary" fontSize="body-s" fontWeight="bold" margin={{ bottom: 'xxs' }}>
                  Routing decision
                </Box>
                <KeyValuePairs columns={1} items={kvItems} />
              </Box>
            )}
          </SpaceBetween>
        </Box>
      )}
    </Box>
  );
}

// ─── StreamingChatBubble ──────────────────────────────────────────────────────
// Shown while a response is being generated. Displays live thinking steps
// and partial response text as they stream in.

export function StreamingIndicator() {
  const {
    isStreaming,
    streamingContent,
    streamingAgent,
    streamingStage,
    streamingThinkingSteps,
    streamingRoutingDecision,
  } = useChatStore();
  if (!isStreaming) return null;

  const agentDisplays = getMultiAgentDisplays(streamingAgent);
  const currentAgentDisplay = agentDisplays.length > 0 ? agentDisplays[agentDisplays.length - 1] : null;

  return (
    <ChatBubble
      type="incoming"
      avatar={
        <Avatar
          color="gen-ai"
          iconName="gen-ai"
          tooltipText={currentAgentDisplay?.label || 'Agent'}
          ariaLabel={currentAgentDisplay?.label || 'Agent'}
        />
      }
      ariaLabel="Agent is processing your request"
      actions={
        <SpaceBetween direction="horizontal" size="xs">
          {agentDisplays.map((display, idx) => (
            <Badge key={idx} color={display.color}>{display.label}</Badge>
          ))}
        </SpaceBetween>
      }
    >
      <SpaceBetween size="s">
        {/* Compact cycling thinking line — shows current step, expands to full history */}
        <ThinkingLine
          steps={streamingThinkingSteps}
          routingDecision={streamingRoutingDecision}
          isStreaming={true}
          streamingStage={streamingStage}
        />

        {/* Response content — shown once tokens start arriving */}
        {streamingContent && (
          <Box variant="p">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {streamingContent}
            </ReactMarkdown>
          </Box>
        )}
      </SpaceBetween>
    </ChatBubble>
  );
}

// ─── MessageDisplay ───────────────────────────────────────────────────────────

interface MessageDisplayProps {
  message: ChatMessage;
}

export function MessageDisplay({ message }: MessageDisplayProps) {
  const isUser = message.sender === 'user';
  const isError = message.agentType?.toLowerCase().includes('error');
  const { openWorkflowPanel } = useWorkflowStore();

  const agentDisplays = getMultiAgentDisplays(message.agentType);
  const primaryAgent = agentDisplays.length > 0 ? agentDisplays[agentDisplays.length - 1] : null;

  const handleWorkflowBadgeClick = () => {
    if (message.workflowId) {
      openWorkflowPanel(message.workflowId, message.isAutomated || false);
    }
  };

  // Build actions (badges) shown on agent messages
  const renderActions = () => {
    if (isUser) return undefined;

    const items: React.ReactNode[] = [];

    // Agent type badge(s)
    agentDisplays.forEach((display, idx) => {
      items.push(
        <Badge key={`agent-${idx}`} color={isError ? 'red' : display.color}>
          {display.label}
        </Badge>
      );
    });

    // Response time
    if (message.responseTimeMs !== undefined) {
      const secs = (message.responseTimeMs / 1000).toFixed(1);
      items.push(
        <Box key="time" color="text-body-secondary" fontSize="body-s" display="inline">
          {secs}s
        </Box>
      );
    }

    // Workflow badge (clickable)
    if (message.workflowTriggered && message.workflowId) {
      items.push(
        <span
          key="workflow"
          onClick={handleWorkflowBadgeClick}
          style={{ cursor: 'pointer' }}
          title="Click to view workflow details"
        >
          <Badge color={message.isAutomated ? 'green' : 'blue'}>
            {message.isAutomated ? 'Auto Workflow' : 'Manual Approval'}
          </Badge>
        </span>
      );
    }

    if (items.length === 0) return undefined;
    return (
      <SpaceBetween direction="horizontal" size="xs">
        {items}
      </SpaceBetween>
    );
  };

  if (isUser) {
    return (
      <ChatBubble
        type="outgoing"
        avatar={
          <Avatar
            color="default"
            iconName="user-profile"
            tooltipText="You"
            ariaLabel="You"
          />
        }
        ariaLabel="Your message"
      >
        <Box variant="p">{message.content}</Box>
      </ChatBubble>
    );
  }

  // Agent message
  return (
    <ChatBubble
      type="incoming"
      avatar={
        <Avatar
          color="gen-ai"
          iconName="gen-ai"
          tooltipText={primaryAgent?.label || 'Agent'}
          ariaLabel={primaryAgent?.label || 'Agent'}
        />
      }
      ariaLabel="Agent response"
      actions={renderActions()}
    >
      <SpaceBetween size="s">
        {/* Main response content with markdown rendering */}
        <Box variant="p">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {message.content}
          </ReactMarkdown>
        </Box>

        {/* Compact thinking line (persisted after completion) */}
        {(message.thinkingSteps && message.thinkingSteps.length > 0) || message.routingDecision ? (
          <ThinkingLine
            steps={message.thinkingSteps || []}
            routingDecision={message.routingDecision}
            isStreaming={false}
            responseTimeMs={message.responseTimeMs}
          />
        ) : null}
      </SpaceBetween>
    </ChatBubble>
  );
}