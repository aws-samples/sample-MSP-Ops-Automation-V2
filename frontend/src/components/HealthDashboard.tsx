// frontend/src/components/HealthDashboard.tsx
/**
 * AWS Health Dashboard sidebar widget.
 *
 * Displays AWS service health events across four categories — Global Outages,
 * Scheduled Maintenance, Notifications, and an Event Summary — fetched from
 * the backend health API endpoints via `apiClient`.
 *
 * Data is **not** fetched automatically on mount; the user must click
 * "Refresh health status" to trigger the first load. This avoids unnecessary
 * API calls (including the AWS Health API, which requires Business/Enterprise
 * support) on every page render.
 *
 * All four API calls are issued in parallel with `Promise.all`. If any
 * individual call returns `success: false`, the non-fatal error note is
 * surfaced in an info `Alert` (common for accounts without the required
 * AWS support tier) rather than crashing the component.
 *
 * The `Global Outages` section auto-expands when there are active events so
 * incidents are immediately visible without user interaction.
 */

import React, { useState, useEffect } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Button from '@cloudscape-design/components/button';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Alert from '@cloudscape-design/components/alert';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Box from '@cloudscape-design/components/box';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import { apiClient } from '../services/api/apiClient';

interface HealthEvent {
  service: string;
  region: string;
  event_type: string;
  status: string;
  description: string;
  start_time?: string;
  metadata?: Record<string, string>;
}

export function HealthDashboard() {
  const [lastUpdated, setLastUpdated] = useState<string>('');
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [outages, setOutages] = useState<HealthEvent[]>([]);
  const [scheduled, setScheduled] = useState<HealthEvent[]>([]);
  const [notifications, setNotifications] = useState<HealthEvent[]>([]);
  const [summary, setSummary] = useState<any>(null);
  const [error, setError] = useState<string>('');

  const refreshHealthData = async () => {
    setIsRefreshing(true);
    setError('');
    
    try {
      // Fetch all health data in parallel
      const [outagesData, scheduledData, notificationsData, summaryData] = await Promise.all([
        apiClient.getHealthOutages(),
        apiClient.getHealthScheduled(),
        apiClient.getHealthNotifications(),
        apiClient.getHealthSummary()
      ]);

      setOutages(outagesData.events || []);
      setScheduled(scheduledData.events || []);
      setNotifications(notificationsData.events || []);
      setSummary(summaryData.data || null);
      
      setLastUpdated(new Date().toLocaleTimeString());
      
      // Check if any request failed (likely missing Business Support plan)
      if (!outagesData.success || !scheduledData.success || !notificationsData.success) {
        setError(outagesData.note || scheduledData.note || notificationsData.note || '');
      }
    } catch (err: any) {
      console.error('Health data refresh failed:', err);
      setError(err.message || 'Failed to fetch health data');
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <Container header={<Header variant="h3">AWS Health Dashboard</Header>}>
      <SpaceBetween size="m">
        {/* Refresh Button */}
        <SpaceBetween direction="horizontal" size="xs">
          <Button 
            iconName="refresh" 
            onClick={refreshHealthData}
            loading={isRefreshing}
          >
            Refresh health status
          </Button>
          {lastUpdated && (
            <Box color="text-body-secondary" fontSize="body-s">
              Last updated: {lastUpdated}
            </Box>
          )}
        </SpaceBetween>

        {/* Error/Info Message */}
        {error && (
          <Alert type="info">
            {error}
          </Alert>
        )}

        {/* Global Outages */}
        <ExpandableSection 
          headerText="Global Outages"
          defaultExpanded={(outages ?? []).length > 0}
        >
          {(outages ?? []).length > 0 ? (
            <SpaceBetween size="s">
              {(outages ?? []).map((event, index) => (
                <Alert
                  key={index}
                  type="warning"
                  header={`${event.service} (${event.region})`}
                >
                  <StatusIndicator type="warning">
                    {event.event_type}
                  </StatusIndicator>
                  <Box margin={{ top: 's' }}>
                    {event.description.substring(0, 200)}...
                  </Box>
                </Alert>
              ))}
            </SpaceBetween>
          ) : (
            <StatusIndicator type="success">No active outages</StatusIndicator>
          )}
        </ExpandableSection>

        {/* Scheduled Maintenance */}
        <ExpandableSection headerText="Scheduled Maintenance">
          {(scheduled ?? []).length > 0 ? (
            <SpaceBetween size="s">
              {(scheduled ?? []).map((event, index) => (
                <Alert
                  key={index}
                  type="info"
                  header={`${event.service} (${event.region})`}
                >
                  <StatusIndicator type="pending">
                    {event.event_type}
                  </StatusIndicator>
                  {event.start_time && (
                    <Box margin={{ top: 's' }} fontSize="body-s">
                      Scheduled: {new Date(event.start_time).toLocaleString()}
                    </Box>
                  )}
                </Alert>
              ))}
            </SpaceBetween>
          ) : (
            <StatusIndicator type="success">No scheduled maintenance</StatusIndicator>
          )}
        </ExpandableSection>

        {/* Notifications */}
        <ExpandableSection headerText="Notifications">
          {(notifications ?? []).length > 0 ? (
            <SpaceBetween size="s">
              {(notifications ?? []).map((event, index) => (
                <Alert
                  key={index}
                  type="info"
                  header={event.service}
                >
                  <StatusIndicator type="info">
                    {event.event_type}
                  </StatusIndicator>
                  <Box margin={{ top: 's' }} fontSize="body-s">
                    {event.description.substring(0, 150)}...
                  </Box>
                </Alert>
              ))}
            </SpaceBetween>
          ) : (
            <StatusIndicator type="success">No new notifications</StatusIndicator>
          )}
        </ExpandableSection>

        {/* Event Summary */}
        <ExpandableSection headerText="Event Summary">
          {summary ? (
            <KeyValuePairs
              columns={1}
              items={[
                {
                  label: 'Total events',
                  value: summary.total.toString()
                },
                {
                  label: 'Issues',
                  value: summary.by_category?.issue ? (
                    <StatusIndicator type="warning">
                      {summary.by_category.issue}
                    </StatusIndicator>
                  ) : '0'
                },
                {
                  label: 'Scheduled changes',
                  value: summary.by_category?.scheduledChange ? (
                    <StatusIndicator type="pending">
                      {summary.by_category.scheduledChange}
                    </StatusIndicator>
                  ) : '0'
                },
                {
                  label: 'Notifications',
                  value: summary.by_category?.accountNotification ? (
                    <StatusIndicator type="info">
                      {summary.by_category.accountNotification}
                    </StatusIndicator>
                  ) : '0'
                }
              ]}
            />
          ) : (
            <Box>Click 'Refresh health status' to check AWS service health</Box>
          )}
        </ExpandableSection>
      </SpaceBetween>
    </Container>
  );
}
