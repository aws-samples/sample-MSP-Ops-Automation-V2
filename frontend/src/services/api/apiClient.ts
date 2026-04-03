// frontend/src/services/api/apiClient.ts
/**
 * Singleton HTTP client for all backend communication.
 *
 * Two complementary request patterns are supported:
 *
 * 1. **SSE streaming** (`sendMessageStream`): submits a chat request, then opens a
 *    `fetch`-based SSE connection to `/chat/:id/stream` routed through CloudFront →
 *    ALB (bypassing API Gateway's 29 s response-buffering limit).  Each SSE event
 *    (`progress`, `agent_switch`, `tool_call`, `tool_result`, `content`, `complete`,
 *    `error`) is forwarded to the caller via `onEvent`.
 *
 * 2. **Poll fallback** (`sendMessage` / `pollForResult`): submits the same request and
 *    then polls `/chat/:id` every 1 s until `status === "complete"`.  Used when SSE is
 *    unavailable or when the stream terminates unexpectedly before a `complete` event.
 *
 * **401 restore flow**: when any request returns 401, the interceptor calls
 * `/auth/restore` (sends the httpOnly refresh cookie) to get a fresh idToken, updates
 * the Authorization header, and retries the original request exactly once.  Concurrent
 * 401 responses are coalesced into a single restore call via `restorePromise` to avoid
 * a token-refresh race.  If restore fails, the user is signed out and redirected to
 * `/login`.
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import { getInMemoryTokens, signOut } from '../auth/cognitoService';
import type { ChatRequest, ChatResponse, ChatPollResponse, Account, SSEProgressEvent } from '../../types';

/** Payload for the two-step account creation flow (Step 3 — finalise with AWS account ID). */
interface AccountCreateRequest {
  account_name: string;
  account_id: string;
  description?: string;
}

/** Response from account creation including the persisted Account record and IAM setup instructions. */
interface AccountCreateResponse {
  success: boolean;
  message: string;
  account: Account;
  setup_instructions: any;
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000/api/v1';

// Stream URL routes through CloudFront → ALB to bypass API Gateway's response buffering.
// In production VITE_STREAM_BASE_URL points to the CloudFront distribution which has a
// cache behavior for api/v1/chat/*/stream that forwards to the ALB directly.
const STREAM_BASE_URL = import.meta.env.VITE_STREAM_BASE_URL || API_BASE_URL;

class APIClient {
  private client: AxiosInstance;
  // Shared promise prevents concurrent 401 restores from racing
  private restorePromise: Promise<any> | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE_URL,
      withCredentials: true, // Send httpOnly refresh cookie on every request
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor — attach in-memory idToken as Bearer
    this.client.interceptors.request.use(
      (config) => {
        const { idToken } = getInMemoryTokens();
        if (idToken) {
          config.headers.Authorization = `Bearer ${idToken}`;
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // Response interceptor — on 401, attempt session restore via cookie then retry once
    this.client.interceptors.response.use(
      (response) => response,
      async (error: AxiosError) => {
        const originalRequest = error.config;

        if (error.response?.status === 401 && originalRequest && !(originalRequest as any)._retry) {
          (originalRequest as any)._retry = true;

          try {
            // Coalesce concurrent 401 restores into a single request
            if (!this.restorePromise) {
              this.restorePromise = axios.post(
                `${API_BASE_URL}/auth/restore`, {},
                { withCredentials: true }
              ).finally(() => { this.restorePromise = null; });
            }
            const restoreRes = await this.restorePromise;
            if (restoreRes.data.success && originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${restoreRes.data.idToken}`;
            }
            return this.client(originalRequest);
          } catch {
            signOut();
            window.location.href = '/login';
            return Promise.reject(error);
          }
        }

        return Promise.reject(error);
      }
    );
  }

  /**
   * Send a chat message using the async poll pattern.
   *
   * Submits the request (returns immediately with `request_id`), then polls
   * `/chat/:id` every 1 s until the backend reports `status === "complete"`.
   * Use this as the guaranteed-safe path when SSE is unavailable.
   *
   * @param request - Chat payload including message, account context, and flags.
   * @param onProgress - Optional callback invoked on each poll that carries a
   *   `progress` object: `{ stage, agent_hint?, message, elapsed_seconds }`.
   * @returns Resolved `ChatResponse` with `{ success, content, agent_type, workflow_triggered?, workflow_id? }`.
   */
  async sendMessage(
    request: ChatRequest,
    onProgress?: (progress: ChatPollResponse['progress']) => void
  ): Promise<ChatResponse> {
    // Step 1: Submit request (returns instantly with request_id)
    const submitRes = await this.client.post('/chat', {
      message: request.message,
      account_name: request.account_name,
      workflow_enabled: request.workflow_enabled,
      full_automation: request.full_automation,
      conversation_id: request.conversation_id,
    });
    const { request_id } = submitRes.data;

    // Step 2: Poll for result every 1 second (reduced from 2s)
    const MAX_POLL_TIME = 300_000; // 5 minutes max
    const POLL_INTERVAL = 1000; // 1 second (reduced from 2s for faster perceived response)
    const startTime = Date.now();
    let consecutiveErrors = 0;
    const MAX_CONSECUTIVE_ERRORS = 3;

    while (Date.now() - startTime < MAX_POLL_TIME) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));

      try {
        const pollRes = await this.client.get(`/chat/${request_id}`);
        const data = pollRes.data as ChatPollResponse;
        
        // Reset error counter on successful poll
        consecutiveErrors = 0;

        // Emit progress to UI
        if (onProgress && data.progress) {
          onProgress(data.progress);
        }

        // Check if complete
        if (data.status === 'complete' && data.result) {
          return data.result;
        }

        // Check if error
        if (data.status === 'error') {
          throw new Error(data.result?.content || 'Chat processing failed');
        }
      } catch (error: any) {
        // Handle transient 504 Gateway Timeouts gracefully
        if (error.response?.status === 504 || error.code === 'ECONNABORTED') {
          consecutiveErrors++;
          console.warn(`Poll attempt failed (${consecutiveErrors}/${MAX_CONSECUTIVE_ERRORS}): ${error.message}`);
          
          if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
            throw new Error('Backend is not responding - please try again later');
          }
          continue;
        }
        
        throw error;
      }
    }

    throw new Error('Request timed out after 5 minutes');
  }

  /**
   * Send a chat message with SSE streaming for real-time progress.
   *
   * Uses `fetch()` + `ReadableStream` instead of `EventSource` because the
   * native `EventSource` API does not support custom request headers, which are
   * required here to pass the JWT Bearer token.
   *
   * The SSE stream URL is routed through CloudFront → ALB to bypass API
   * Gateway's 29 s response-buffering limit (see `STREAM_BASE_URL`).
   *
   * Falls back to `pollForResult()` if the stream connection fails or terminates
   * without a `complete` event.  If polling also fails, both error messages are
   * surfaced together so the caller can diagnose which layer broke.
   *
   * @param request - Chat payload including message, account context, and flags.
   * @param onEvent - Callback invoked for every parsed SSE event.  Event types:
   *   `progress` | `agent_switch` | `tool_call` | `tool_result` | `content` |
   *   `complete` | `error`.
   * @returns Resolved `ChatResponse` once a `complete` event is received (or poll succeeds).
   */
  async sendMessageStream(
    request: ChatRequest,
    onEvent: (event: SSEProgressEvent) => void
  ): Promise<ChatResponse> {
    // Step 1: Submit request (returns instantly with request_id)
    const submitRes = await this.client.post('/chat', {
      message: request.message,
      account_name: request.account_name,
      workflow_enabled: request.workflow_enabled,
      full_automation: request.full_automation,
      conversation_id: request.conversation_id,
    });
    const { request_id } = submitRes.data;

    // Step 2: Connect to SSE stream
    const { idToken } = getInMemoryTokens();
    if (!idToken) {
      // No token — fall back to poll
      return this.sendMessage(request);
    }

    const streamUrl = `${STREAM_BASE_URL}/chat/${request_id}/stream`;

    try {
      const response = await fetch(streamUrl, {
        method: 'GET',
        credentials: 'include', // send httpOnly cookie
        headers: {
          'Authorization': `Bearer ${idToken}`,
          'Accept': 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
      });

      if (!response.ok || !response.body) {
        console.warn('SSE stream unavailable, falling back to poll');
        return this.pollForResult(request_id, onEvent);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      // IMPORTANT: currentEvent/currentData must be OUTSIDE the while loop
      // so they persist across chunk boundaries when SSE events span multiple chunks
      let currentEvent = '';
      let currentData = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        
        // Parse SSE lines from buffer
        const lines = buffer.split('\n');
        buffer = lines.pop() || ''; // Keep incomplete last line in buffer

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            currentData = line.slice(6).trim();
          } else if (line === '' && currentEvent && currentData) {
            // Complete SSE event — parse and dispatch
            let parsedData: any;
            try {
              parsedData = JSON.parse(currentData);
            } catch {
              console.warn('Failed to parse SSE event data:', currentData);
              currentEvent = '';
              currentData = '';
              continue;
            }

            const sseEvent: SSEProgressEvent = {
              event: currentEvent as SSEProgressEvent['event'],
              data: parsedData,
              request_id,
            };
            onEvent(sseEvent);

            // Return on complete event
            if (currentEvent === 'complete' && parsedData.result) {
              reader.cancel();
              return parsedData.result as ChatResponse;
            }

            // Throw on error event (must be outside try/catch to propagate correctly)
            if (currentEvent === 'error') {
              reader.cancel();
              throw new Error(parsedData.message || 'Stream error');
            }

            currentEvent = '';
            currentData = '';
          }
        }
      }

      // Flush any pending event that wasn't followed by a blank line
      if (currentEvent && currentData) {
        try {
          const parsedData = JSON.parse(currentData);
          const sseEvent: SSEProgressEvent = {
            event: currentEvent as SSEProgressEvent['event'],
            data: parsedData,
            request_id,
          };
          onEvent(sseEvent);
          if (currentEvent === 'complete' && parsedData.result) {
            return parsedData.result as ChatResponse;
          }
        } catch {
          // ignore parse error on trailing event
        }
      }

      // Stream ended without complete event — fall back to poll
      return this.pollForResult(request_id, onEvent);

    } catch (streamError: any) {
      console.warn('SSE streaming failed, falling back to poll:', streamError.message);
      try {
        return await this.pollForResult(request_id, onEvent);
      } catch (pollError: any) {
        throw new Error(`Streaming failed (${streamError.message}) and polling also failed (${pollError.message})`);
      }
    }
  }

  /**
   * Poll `/chat/:id` until the request completes (SSE fallback).
   *
   * Emits synthetic `progress` and `agent_switch` SSE events through `onEvent`
   * so the streaming indicator in the UI stays active even when the real SSE
   * stream is unavailable.  Silently retries on transient 504 / connection errors.
   *
   * @param requestId - The `request_id` returned by the initial POST to `/chat`.
   * @param onEvent - Optional SSE-shaped event callback (same signature as `sendMessageStream`).
   * @returns Resolved `ChatResponse` when `status === "complete"`.
   */
  private async pollForResult(
    requestId: string,
    onEvent?: (event: SSEProgressEvent) => void
  ): Promise<ChatResponse> {
    const MAX_POLL_TIME = 300_000;
    const POLL_INTERVAL = 1000;
    const startTime = Date.now();
    let lastStage = '';

    while (Date.now() - startTime < MAX_POLL_TIME) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));
      try {
        const pollRes = await this.client.get(`/chat/${requestId}`);
        const data = pollRes.data as ChatPollResponse;

        // Emit progress update if stage changed
        if (onEvent && data.progress) {
          const stage = data.progress.stage || '';
          const message = data.progress.message || '';
          if (stage !== lastStage && message) {
            lastStage = stage;
            onEvent({
              event: 'progress',
              data: { stage, message },
              request_id: requestId,
            });
          }
        }

        if (data.status === 'complete' && data.result) {
          // Emit agent_switch on completion so badge shows correct agent
          if (onEvent && data.result.agent_type) {
            onEvent({
              event: 'agent_switch',
              data: { to_agent: data.result.agent_type },
              request_id: requestId,
            });
          }
          return data.result;
        }
        if (data.status === 'error') throw new Error(data.result?.content || 'Chat failed');
      } catch (error: any) {
        if (error.response?.status === 504 || error.code === 'ECONNABORTED') continue;
        throw error;
      }
    }
    throw new Error('Request timed out after 5 minutes');
  }

  /**
   * Load chat history from AgentCore Memory.
   *
   * Called on page load to restore the previous session so the user sees
   * continuity across refreshes.  AgentCore scopes LTM to account and STM to
   * the active `conversation_id`.
   *
   * @param k - Maximum number of messages to return (default 10).
   * @param conversationId - Restrict history to a specific conversation session.
   * @returns Array of messages ordered oldest-first, each with `{ id, sender, content, agentType?, timestamp }`.
   */
  async getChatHistory(k = 10, conversationId?: string): Promise<Array<{
    id: string;
    sender: 'user' | 'agent';
    content: string;
    agentType?: string | null;
    timestamp: string;
  }>> {
    let url = `/chat/history?k=${k}`;
    if (conversationId) {
      url += `&conversation_id=${encodeURIComponent(conversationId)}`;
    }
    const response = await this.client.get(url);
    return response.data.messages || [];
  }

  /**
   * Get current user info
   */
  async getUserInfo() {
    const response = await this.client.get('/me');
    return response.data;
  }

  /**
   * Get MSP principal ARN and account ID
   * Used for populating AWS CLI commands with actual MSP ARN
   */
  async getMspPrincipal(): Promise<{ success: boolean; principal_arn: string; account_id: string }> {
    const response = await this.client.get('/msp-principal');
    return response.data;
  }

  /**
   * Get available accounts
   */
  async getAccounts(): Promise<Account[]> {
    const response = await this.client.get<{ accounts: Account[] }>('/accounts');
    return response.data.accounts;
  }

  /**
   * Prepare account (Step 1→2) - generates external_id and role_name
   */
  async prepareAccount(accountName: string): Promise<{
    success: boolean;
    existing: boolean;
    account_name: string;
    role_name: string;
    external_id: string;
    msp_principal_arn: string;
    account_id?: string;
  }> {
    const response = await this.client.post('/accounts/prepare', {
      account_name: accountName
    });
    return response.data;
  }

  /**
   * Create new customer account (Step 3) - completes prepared account with account_id
   */
  async createAccount(request: AccountCreateRequest): Promise<AccountCreateResponse> {
    const response = await this.client.post<AccountCreateResponse>('/accounts', {
      account_name: request.account_name,
      account_id: request.account_id,
      description: request.description
    });
    return response.data;
  }

  /**
   * Delete customer account
   */
  async deleteAccount(accountName: string): Promise<{ success: boolean; message: string }> {
    const response = await this.client.delete(`/accounts/${encodeURIComponent(accountName)}`);
    return response.data;
  }

  /**
   * Refresh account tokens
   */
  async refreshAccount(accountName: string): Promise<{ success: boolean; message: string; access_test?: any }> {
    const response = await this.client.put(`/accounts/${encodeURIComponent(accountName)}/refresh`);
    return response.data;
  }

  /**
   * Refresh ALL account tokens
   */
  async refreshAllAccounts(): Promise<{ 
    success: boolean; 
    refreshed: number; 
    failed: number; 
    message: string;
    results?: Array<{
      account: string;
      status: string;
      message: string;
      error?: string;
    }>;
  }> {
    const response = await this.client.post('/accounts/refresh-all');
    return response.data;
  }

  /**
   * Switch to different account context
   */
  async switchAccount(accountName: string): Promise<{ success: boolean; message: string; account_context?: string }> {
    const response = await this.client.post(`/accounts/${encodeURIComponent(accountName)}/switch`);
    return response.data;
  }

  /**
   * Health check (protected endpoint)
   */
  async healthCheck() {
    const response = await this.client.get('/health/protected');
    return response.data;
  }

  /**
   * Get AWS Health event summary
   */
  async getHealthSummary(): Promise<any> {
    const response = await this.client.get('/health/summary');
    return response.data;
  }

  /**
   * Get active AWS service outages
   */
  async getHealthOutages(): Promise<any> {
    const response = await this.client.get('/health/outages');
    return response.data;
  }

  /**
   * Get scheduled AWS maintenance
   */
  async getHealthScheduled(): Promise<any> {
    const response = await this.client.get('/health/scheduled');
    return response.data;
  }

  /**
   * Get AWS account notifications
   */
  async getHealthNotifications(): Promise<any> {
    const response = await this.client.get('/health/notifications');
    return response.data;
  }

  /**
   * Get pending workflow approvals
   */
  async getPendingWorkflows(): Promise<any> {
    const response = await this.client.get('/workflows/pending');
    return response.data;
  }

  /**
   * Approve a workflow step using the async poll pattern to prevent API Gateway timeout.
   *
   * Submits approval immediately (returns `request_id`), then delegates to
   * `pollWorkflowStep` which tails `streaming_events` from the chat poll endpoint
   * so `onStepProgress` receives incremental step updates in real time.
   *
   * @param workflowId - The workflow to approve.
   * @param stepType - Step identifier, e.g. `"full_auto"`, `"jira"`, `"remediation"`.
   * @param onStepProgress - Optional callback invoked for each new `workflow_step` event
   *   arriving from `streaming_events`; useful for live-updating `fullAutoStatus` in the store.
   * @returns Final result object from the backend once all steps complete.
   */
  async approveWorkflowStep(workflowId: string, stepType: string, onStepProgress?: (step: any) => void): Promise<any> {
    // Step 1: Submit approval (returns instantly with request_id)
    const submitRes = await this.client.post(`/workflows/${workflowId}/approve/${stepType}`);
    const { request_id } = submitRes.data;

    // Step 2: Poll for result using same endpoint as chat
    return this.pollWorkflowStep(request_id, onStepProgress);
  }

  /**
   * Poll for workflow step result (reuses chat polling endpoint)
   */
  private async pollWorkflowStep(requestId: string, onStepProgress?: (step: any) => void): Promise<any> {
    const MAX_POLL_TIME = 300_000; // 5 minutes max
    const POLL_INTERVAL = 2000; // 2 seconds
    const startTime = Date.now();
    let lastSeenSteps = 0;

    while (Date.now() - startTime < MAX_POLL_TIME) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL));

      try {
        const pollRes = await this.client.get(`/chat/${requestId}`);
        const data = pollRes.data;

        // Emit incremental step progress events
        const events = data.streaming_events || [];
        if (onStepProgress && events.length > lastSeenSteps) {
          for (const evt of events.slice(lastSeenSteps)) {
            if (evt.data?.type === 'workflow_step') {
              onStepProgress(evt.data);
            }
          }
          lastSeenSteps = events.length;
        }

        // Check if complete
        if (data.status === 'complete' && data.result) {
          return data.result;
        }

        // Check if error
        if (data.status === 'error') {
          throw new Error(data.result?.content || 'Workflow step failed');
        }
      } catch (error: any) {
        // Handle transient errors gracefully
        if (error.response?.status === 504 || error.code === 'ECONNABORTED') {
          console.warn(`Poll attempt failed: ${error.message}`);
          continue;
        }
        throw error;
      }
    }

    throw new Error('Workflow step timed out after 5 minutes');
  }

  /**
   * Reject workflow step
   */
  async rejectWorkflowStep(workflowId: string, stepType: string): Promise<any> {
    const response = await this.client.post(`/workflows/${workflowId}/reject/${stepType}`);
    return response.data;
  }

  /**
   * Get workflow status
   */
  async getWorkflowStatus(workflowId: string): Promise<any> {
    const response = await this.client.get(`/workflows/${workflowId}/status`);
    return response.data;
  }

  /**
   * Execute full workflow automation (async pattern)
   */
  async executeFullAutomation(workflowId: string): Promise<any> {
    const response = await this.client.post(`/workflows/${workflowId}/automate`);
    return response.data;
  }

  /**
   * Get automation progress
   */
  async getAutomationProgress(workflowId: string): Promise<any> {
    const response = await this.client.get(`/workflows/${workflowId}/progress`);
    return response.data;
  }
}

// Export singleton instance
export const apiClient = new APIClient();
