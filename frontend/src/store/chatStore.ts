// frontend/src/store/chatStore.ts
/**
 * Chat state and streaming accumulator for the MSP assistant conversation.
 *
 * Holds two distinct layers of state:
 *
 * 1. **Conversation state** (`messages`, `conversationId`, `inputValue`, `isLoading`) —
 *    the durable record of the conversation.  `conversationId` is generated once per
 *    session and scopes AgentCore Short-Term Memory (STM) to this browser tab.
 *    `clearMessages` rotates the ID so a new STM context begins.
 *
 * 2. **Streaming accumulator** (`isStreaming`, `streamingContent`, `streamingAgent`,
 *    `streamingStage`, `streamingThinkingSteps`, `streamingRoutingDecision`) — transient
 *    state populated as SSE events arrive.  All fields are reset by `clearStreaming`
 *    once the response is committed to `messages`.
 *
 * Thinking data ownership:
 * `streamingThinkingSteps` and `streamingRoutingDecision` are accumulated during the
 * SSE stream.  Ownership transfers to a `ChatMessage` record via `getAndClearThinkingData`,
 * which reads and atomically clears the accumulator.  The caller (the send-message
 * handler) is responsible for attaching the returned data to the final message before
 * calling `addMessage`; no other code path should consume these fields.
 */

import { create } from 'zustand';
import type { ChatMessage, ThinkingStep, RoutingDecision } from '../types';

// Generate a unique conversation ID
const generateConversationId = (): string => {
  return crypto.randomUUID();
};

interface ChatState {
  /** Committed conversation history rendered in the chat panel. */
  messages: ChatMessage[];
  /** Controlled value of the chat input field. */
  inputValue: string;
  /** True while a request is in-flight (disables the send button). */
  isLoading: boolean;
  /**
   * Stable UUID for the current conversation session.
   * Sent with every request so AgentCore can scope STM to this tab.
   * Rotated on `clearMessages` to start a fresh memory context.
   */
  conversationId: string;

  // SSE Streaming state  shows real-time progress to users
  /** True from first SSE event until `clearStreaming` is called after commit. */
  isStreaming: boolean;
  /** Partial response text as `content` SSE chunks arrive. */
  streamingContent: string;
  /** Name of the specialist agent currently handling the request (from `agent_switch` events). */
  streamingAgent: string;
  /** Human-readable stage label from the latest `progress` SSE event. */
  streamingStage: string;

  // Thinking/reasoning accumulator  captures SSE events as thinking steps
  /** Ordered list of thinking steps collected during the current stream; cleared by `getAndClearThinkingData`. */
  streamingThinkingSteps: ThinkingStep[];
  /** Routing decision extracted from the supervisor `agent_switch` event; cleared by `getAndClearThinkingData`. */
  streamingRoutingDecision: RoutingDecision | null;

  /** Append a single committed message to the conversation history. */
  addMessage: (message: ChatMessage) => void;
  /** Replace the full message list (used when restoring history from AgentCore Memory). */
  setMessages: (messages: ChatMessage[]) => void;
  /** Update the chat input field value. */
  setInputValue: (value: string) => void;
  /** Set the loading flag (true while a request is in-flight). */
  setLoading: (loading: boolean) => void;
  /** Clear conversation history and rotate the conversation ID to start a fresh STM context. */
  clearMessages: () => void;

  // Streaming actions
  /** Replace the entire streaming content buffer (used on first content event). */
  setStreamingContent: (content: string) => void;
  /** Append a text chunk to the streaming content buffer (called for each `content` SSE chunk). */
  appendStreamingContent: (chunk: string) => void;
  /** Update the displayed agent name (called on `agent_switch` SSE events). */
  setStreamingAgent: (agent: string) => void;
  /** Update the displayed stage label (called on `progress` SSE events). */
  setStreamingStage: (stage: string) => void;
  /** Toggle the streaming-in-progress flag. */
  setStreaming: (streaming: boolean) => void;
  /** Reset all transient streaming state once a response has been committed to `messages`. */
  clearStreaming: () => void;

  // Thinking step actions
  /** Append a captured SSE event as a thinking step for the current stream. */
  appendThinkingStep: (step: ThinkingStep) => void;
  /** Record the supervisor routing decision extracted from an `agent_switch` SSE event. */
  setRoutingDecision: (decision: RoutingDecision) => void;
  /**
   * Returns accumulated thinking data and clears it from state.
   * Call this after response completes to attach steps to the final ChatMessage.
   */
  getAndClearThinkingData: () => { steps: ThinkingStep[]; routing: RoutingDecision | null };
}

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  inputValue: '',
  isLoading: false,
  conversationId: generateConversationId(),

  // Streaming initial state
  isStreaming: false,
  streamingContent: '',
  streamingAgent: '',
  streamingStage: '',

  // Thinking accumulator initial state
  streamingThinkingSteps: [],
  streamingRoutingDecision: null,

  addMessage: (message) =>
    set((state) => ({
      messages: [...state.messages, message],
    })),

  setMessages: (messages) => set({ messages }),

  setInputValue: (value) => set({ inputValue: value }),

  setLoading: (loading) => set({ isLoading: loading }),

  clearMessages: () => set({
    messages: [],
    inputValue: '',
    conversationId: generateConversationId(),
    isStreaming: false,
    streamingContent: '',
    streamingAgent: '',
    streamingStage: '',
    streamingThinkingSteps: [],
    streamingRoutingDecision: null,
  }),

  // Streaming actions
  setStreamingContent: (content) => set({ streamingContent: content }),
  appendStreamingContent: (chunk) => set((state) => ({
    streamingContent: state.streamingContent + chunk
  })),
  setStreamingAgent: (agent) => set({ streamingAgent: agent }),
  setStreamingStage: (stage) => set({ streamingStage: stage }),
  setStreaming: (streaming) => set({ isStreaming: streaming }),
  clearStreaming: () => set({
    isStreaming: false,
    streamingContent: '',
    streamingAgent: '',
    streamingStage: '',
    streamingThinkingSteps: [],
    streamingRoutingDecision: null,
  }),

  // Thinking step actions
  appendThinkingStep: (step) =>
    set((state) => ({
      streamingThinkingSteps: [...state.streamingThinkingSteps, step],
    })),

  setRoutingDecision: (decision) => set({ streamingRoutingDecision: decision }),

  getAndClearThinkingData: () => {
    const { streamingThinkingSteps, streamingRoutingDecision } = get();
    // Clear accumulated data
    set({ streamingThinkingSteps: [], streamingRoutingDecision: null });
    return { steps: streamingThinkingSteps, routing: streamingRoutingDecision };
  },
}));