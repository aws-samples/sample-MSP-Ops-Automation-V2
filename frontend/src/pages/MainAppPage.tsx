// frontend/src/pages/MainAppPage.tsx
/**
 * Main application page — chat interface with live SSE streaming.
 *
 * Layout (Cloudscape AppLayout):
 * - **Content** — chat history (`MessageDisplay`), live `StreamingIndicator`,
 *   and a multi-line `PromptInput` for composing messages.
 * - **Navigation** (left) — `NavigationPanel` with account selector, workflow
 *   toggles, sample questions, and the health dashboard.
 * - **Tools panel** (right) — `WorkflowPanel`, shown only when a workflow is
 *   active (`activeWorkflowId` is set).
 *
 * Message flow:
 * 1. User submits a message → `apiClient.sendMessageStream` opens an SSE
 *    connection to the backend.
 * 2. SSE events (`progress`, `agent_switch`, `tool_call`, `content`) are
 *    processed by `handleSSEEvent`, which updates the live streaming state and
 *    accumulates `ThinkingStep` records in `chatStore`.
 * 3. When the stream ends, thinking steps are snapshotted onto the final
 *    `ChatMessage` object so `ThinkingLine` can render them after the fact.
 * 4. If the response included a triggered workflow, `WorkflowPanel` is opened
 *    automatically.
 *
 * The `AGENT_KEYWORDS` map mirrors the backend's `_build_routing_reason()`
 * and is used as a client-side fallback when the SSE event omits
 * `routing_reason`.
 *
 * `stepCounter` is a module-level integer — reset only on full page reload —
 * used to generate IDs that are unique within a browser session.
 */

import React, { useRef, useEffect } from 'react';
import AppLayout from '@cloudscape-design/components/app-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import Box from '@cloudscape-design/components/box';
import PromptInput from '@cloudscape-design/components/prompt-input';
import { useChatStore } from '../store/chatStore';
import { useWorkflowStore } from '../store/workflowStore';
import { useAccountStore } from '../store/accountStore';
import { apiClient } from '../services/api/apiClient';
import { MessageDisplay, StreamingIndicator } from '../components/MessageDisplay';
import { NavigationPanel } from '../components/NavigationPanel';
import WorkflowPanel from '../components/WorkflowPanel';
import type { ChatMessage, SSEProgressEvent, ThinkingStep } from '../types';

// Counter for generating unique step IDs
let stepCounter = 0;
function makeStepId() {
  return `step-${++stepCounter}-${Date.now()}`;
}

// Keyword-based routing reason — mirrors backend _build_routing_reason()
// Used as fallback when backend doesn't provide routing_reason in SSE event
const AGENT_KEYWORDS: Record<string, string[]> = {
  cost:        ['cost', 'spend', 'bill', 'budget', 'pricing', 'expense', 'saving'],
  cloudwatch:  ['alarm', 'cloudwatch', 'metric', 'log', 'monitor', 'performance', 'cpu', 'memory'],
  security:    ['security', 'finding', 'compliance', 'vulnerability', 'securityhub'],
  advisor:     ['advisor', 'best practice', 'recommendation', 'trusted', 'optimize'],
  jira:        ['jira', 'ticket', 'issue', 'incident'],
  knowledge:   ['troubleshoot', 'how to', 'guide', 'kb', 'fix', 'resolve'],
};

function buildRoutingReason(message: string, agentHint: string): string {
  const msg = message.toLowerCase();
  const keywords = AGENT_KEYWORDS[agentHint] || [];
  const matched = keywords.filter(k => msg.includes(k));
  if (matched.length > 0) return `Keywords detected: ${matched.slice(0, 4).join(', ')}`;
  return `${agentHint} domain query`;
}

function MainAppPage() {
  const {
    messages, inputValue, isLoading, conversationId,
    addMessage, setInputValue, setLoading, clearMessages,
    setStreaming, setStreamingStage, setStreamingAgent,
    appendStreamingContent, clearStreaming,
    appendThinkingStep, setRoutingDecision, getAndClearThinkingData,
  } = useChatStore();
  const { smartWorkflowsEnabled, fullAutomationEnabled, setPendingWorkflowId, openWorkflowPanel, closeWorkflowPanel, isOpen: workflowPanelOpen, activeWorkflowId } = useWorkflowStore();
  const { selectedAccount } = useAccountStore();
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new messages arrive or streaming updates
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, isLoading]);

  const handleSendMessage = async () => {
    if (!inputValue.trim() || isLoading) return;

    const userMessage: ChatMessage = {
      id: Date.now().toString(),
      content: inputValue,
      sender: 'user',
      timestamp: new Date(),
    };

    addMessage(userMessage);
    const messageText = inputValue;
    setInputValue('');
    setLoading(true);
    setStreaming(true);
    setStreamingStage('Analyzing your request...');
    const requestStartTime = Date.now();

    try {
      // Track which specialist agents have had their first content chunk captured
      const capturedAgents = new Set<string>();

      // SSE streaming handler — updates live progress panel AND accumulates thinking steps
      const handleSSEEvent = (event: SSEProgressEvent) => {
        switch (event.event) {
          case 'progress': {
            const msg = event.data.message || event.data.stage || '';
            setStreamingStage(msg);
            // Capture as a thinking step (routing/progress type)
            if (msg) {
              appendThinkingStep({
                id: makeStepId(),
                type: 'progress',
                message: msg,
                timestamp: Date.now(),
                status: 'success',
              });
            }
            break;
          }

          case 'agent_switch': {
            const toAgent = event.data.to_agent || '';
            const fromAgent = event.data.from_agent || 'supervisor';
            // Use backend-provided routing_reason, fall back to local keyword detection
            const backendReason = (event.data as any).routing_reason || '';
            const routingReason = backendReason || buildRoutingReason(messageText, toAgent);
            const switchMsg = routingReason
              ? `Routing to ${toAgent}: ${routingReason}`
              : (event.data.message || `Delegating to ${toAgent}...`);
            setStreamingAgent(toAgent);
            setStreamingStage(switchMsg);

            // Capture agent switch with routing reason
            appendThinkingStep({
              id: makeStepId(),
              type: 'agent_switch',
              message: switchMsg,
              agentName: toAgent,
              routingReason,
              timestamp: Date.now(),
              status: 'success',
            });

            // Build routing decision
            setRoutingDecision({
              queryType: messageText.length > 80 ? messageText.slice(0, 80) + '...' : messageText,
              selectedAgent: toAgent,
              alternativeAgents: fromAgent !== 'supervisor' ? fromAgent : '',
              routingReason,
              timestamp: Date.now(),
            });
            break;
          }

          case 'tool_call': {
            const toolName = event.data.tool_name || 'tool';
            const isResult = event.data.status === 'complete';
            const resultPreview = (event.data as any).result_preview || '';
            // routing_reason on tool_call is the actual query snippet sent to specialist
            const toolQuery = (event.data as any).routing_reason || messageText.slice(0, 80);

            if (isResult) {
              // Specialist responded — show first 150 chars of response as preview
              const agentName = (event.data as any).agent || 'Agent';
              const resultMsg = resultPreview
                ? `${agentName} responded: "${resultPreview.slice(0, 120)}${resultPreview.length > 120 ? '...' : ''}"`
                : `${agentName} response received`;
              setStreamingStage(resultMsg);
              appendThinkingStep({
                id: makeStepId(),
                type: 'tool_result',
                message: resultMsg,
                agentName,
                timestamp: Date.now(),
                status: 'success',
              });
            } else {
              // Tool being called — show the query sent to specialist
              const toolMsg = `${toolName}: "${toolQuery}"`;
              setStreamingStage(`${toolName} executing...`);
              appendThinkingStep({
                id: makeStepId(),
                type: 'tool_call',
                message: toolMsg,
                toolName,
                timestamp: Date.now(),
                status: 'in-progress',
              });
            }
            break;
          }

          case 'content': {
            if (event.data.text) {
              appendStreamingContent(event.data.text);
              const isReasoning = (event.data as any).is_reasoning === true;
              const agentType = event.data.agent_type || '';
              const text = event.data.text.trim();

              const addContentStep = (
                agentName: string,
                messagePrefix: string,
                status: 'success' | 'in-progress',
              ) => {
                if (text.length <= 5) return;
                const preview = text.length > 120 ? text.slice(0, 120) + '...' : text;
                appendThinkingStep({
                  id: makeStepId(),
                  type: 'content',
                  message: messagePrefix ? `${messagePrefix}: ${preview}` : preview,
                  agentName,
                  timestamp: Date.now(),
                  status,
                });
              };

              if (isReasoning || agentType === 'supervisor') {
                addContentStep('supervisor', '', 'success');
              } else if (agentType && !capturedAgents.has(agentType)) {
                capturedAgents.add(agentType);
                addContentStep(agentType, agentType, 'in-progress');
              }
            }
            if (event.data.agent_type) setStreamingAgent(event.data.agent_type);
            break;
          }

          default:
            break;
        }
      };

      // SSE streaming (falls back to poll internally if unavailable)
      const response = await apiClient.sendMessageStream(
        {
          message: messageText,
          account_name: selectedAccount?.id || 'default',
          workflow_enabled: smartWorkflowsEnabled,
          full_automation: fullAutomationEnabled,
          conversation_id: conversationId,
        },
        handleSSEEvent
      );

      // Capture accumulated thinking data before clearing
      const { steps: thinkingSteps, routing: routingDecision } = getAndClearThinkingData();
      clearStreaming();

      const responseTimeMs = Date.now() - requestStartTime;
      const agentMessage: ChatMessage = {
        id: (Date.now() + 1).toString(),
        content: response.content,
        sender: 'agent',
        agentType: response.agent_type,
        timestamp: new Date(),
        responseTimeMs,
        workflowTriggered: response.workflow_triggered,
        workflowId: response.workflow_id || undefined,
        requiresApproval: response.workflow_triggered && !fullAutomationEnabled,
        isAutomated: response.workflow_triggered && fullAutomationEnabled,
        // Attach thinking steps so ThinkingDropdown persists in the completed message
        thinkingSteps: thinkingSteps.length > 0 ? thinkingSteps : undefined,
        routingDecision: routingDecision || undefined,
      };

      addMessage(agentMessage);

      // Open workflow panel and start polling when a workflow was triggered
      console.log('[Workflow]', { triggered: response.workflow_triggered, workflow_id: response.workflow_id, toggleEnabled: smartWorkflowsEnabled });
      if (response.workflow_triggered && response.workflow_id) {
        setPendingWorkflowId(response.workflow_id);
        openWorkflowPanel(response.workflow_id, fullAutomationEnabled);
      }
    } catch (error: any) {
      const { steps: errorThinkingSteps, routing: errorRouting } = getAndClearThinkingData();
      clearStreaming();
      const errorMessage: ChatMessage = {
        id: (Date.now() + 1).toString(),
        content: `Error: ${error.message || 'Failed to get response'}`,
        sender: 'agent',
        agentType: 'Error',
        timestamp: new Date(),
        thinkingSteps: errorThinkingSteps.length > 0 ? errorThinkingSteps : undefined,
        routingDecision: errorRouting || undefined,
      };
      addMessage(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  // PromptInput fires onAction when user hits the send button or Enter
  const handleAction = () => {
    handleSendMessage();
  };

  return (
    <AppLayout
      content={
        <SpaceBetween size="l">
          <Header
            description="AI-powered operations assistant for AWS MSP teams"
            variant="h1"
          >
            AWS MSP Smart Agent Assist
          </Header>

          <Container>
            <SpaceBetween size="l">
              {/* Welcome screen — shown before first message */}
              {messages.length === 0 && (
                <Box
                  color="text-body-secondary"
                  padding={{ horizontal: 'l', vertical: 'xxl' }}
                  textAlign="center"
                >
                  <Box padding={{ bottom: 's' }} variant="h2">
                    Welcome to Smart Agent Assist
                  </Box>
                  <Box variant="p">
                    Ask me about CloudWatch monitoring or get troubleshooting help.
                    Use the sidebar for sample questions.
                  </Box>
                </Box>
              )}

              {/* Message history with ChatBubble rendering */}
              {messages.length > 0 && (
                <SpaceBetween size="m">
                  {messages.map((message) => (
                    <MessageDisplay key={message.id} message={message} />
                  ))}
                </SpaceBetween>
              )}

              {/* SSE Streaming indicator — live ChatBubble with thinking dropdown */}
              <StreamingIndicator />

              {/* Scroll anchor */}
              <div ref={messagesEndRef} />

              {/* PromptInput — multi-line with embedded send button */}
              <PromptInput
                value={inputValue}
                onChange={({ detail }) => setInputValue(detail.value)}
                onAction={handleAction}
                actionButtonIconName="send"
                actionButtonAriaLabel="Send message"
                ariaLabel="Message input"
                placeholder="Ask about AWS problems, troubleshoot issues, create ITSM tickets, and resolve incidents..."
                maxRows={5}
                minRows={1}
                disabled={isLoading}
              />

              {/* Tips */}
              <Box color="text-body-secondary" fontSize="body-s" padding={{ top: 'm' }}>
                <SpaceBetween size="xs">
                  <Box fontWeight="bold">Tips:</Box>
                  <Box>• Ask about CloudWatch alarms, metrics, logs for monitoring questions</Box>
                  <Box>• Ask &apos;How to troubleshoot...&apos; or &apos;Steps to resolve...&apos; for knowledge base help</Box>
                  <Box>• Ask &apos;Create ticket...&apos; or &apos;File bug...&apos; for Jira ticket creation</Box>
                  <Box>• Enable Smart Workflows in sidebar for automated CloudWatch → Jira ticket creation</Box>
                  <Box>• Enable Full Automation for zero-click incident response with all steps executing automatically</Box>
                </SpaceBetween>
              </Box>
            </SpaceBetween>
          </Container>
        </SpaceBetween>
      }
      navigation={<NavigationPanel />}
      navigationWidth={320}
      tools={<WorkflowPanel />}
      toolsWidth={350}
      toolsOpen={workflowPanelOpen && !!activeWorkflowId}
      onToolsChange={({ detail }) => { if (!detail.open) closeWorkflowPanel(); }}
      toolsHide={!activeWorkflowId}
    />
  );
}

export default MainAppPage;