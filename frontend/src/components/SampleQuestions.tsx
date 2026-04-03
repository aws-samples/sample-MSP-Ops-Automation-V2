// frontend/src/components/SampleQuestions.tsx
/**
 * Sample questions panel.
 *
 * Renders a set of pre-written questions grouped by AWS service domain
 * (Supervisor, Cost Explorer, Security Hub, Trusted Advisor, CloudWatch,
 * Knowledge Base, Jira). Each group is an `ExpandableSection`; the
 * CloudWatch section is open by default because it is the most commonly
 * used domain.
 *
 * Clicking any question link calls `onQuestionClick` with the question
 * text, which the parent (`NavigationPanel`) forwards to `chatStore` so
 * the text appears in the `PromptInput` ready to send.
 *
 * @param onQuestionClick - Callback invoked with the question string when
 *   the user clicks a sample question link.
 */

import React from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Link from '@cloudscape-design/components/link';

interface SampleQuestionsProps {
  onQuestionClick: (question: string) => void;
}

export function SampleQuestions({ onQuestionClick }: SampleQuestionsProps) {
  const questionCategories = [
    {
      title: 'Supervisor (Multi-Service)',
      questions: [
        'Show me security findings that might impact costs',
        'What performance issues are driving up my spending?',
        'Give me a complete AWS environment health check',
      ],
      defaultExpanded: false,
    },
    {
      title: 'Cost Explorer',
      questions: [
        "What's my AWS spending over the last 4 months?",
        'Show me cost trends for the past 6 months',
        'What are my top cost optimization opportunities?',
        'Year to date costs by service',
      ],
      defaultExpanded: false,
    },
    {
      title: 'Security Hub',
      questions: [
        'Show me critical security findings',
        "What's my compliance status for AWS FSBP?",
        'Security recommendations prioritized by impact',
      ],
      defaultExpanded: false,
    },
    {
      title: 'Trusted Advisor',
      questions: [
        'Show me Trusted Advisor recommendations',
        'What are my Trusted Advisor check results?',
        'Give me Trusted Advisor insights by category',
      ],
      defaultExpanded: false,
    },
    {
      title: 'CloudWatch',
      questions: [
        'Do I have any active alarms?',
        'Show me my log groups',
        'Health status of my EC2 instances',
      ],
      defaultExpanded: true,
    },
    {
      title: 'Knowledge Base',
      questions: [
        'How to troubleshoot high CPU for EC2?',
        'Steps to resolve memory issues',
        'How to fix API Gateway 4xx errors?',
      ],
      defaultExpanded: false,
    },
    {
      title: 'Jira',
      questions: [
        'Create a bug ticket for login issue',
        'Create task for database optimization',
        'Create security incident ticket for critical findings',
      ],
      defaultExpanded: false,
    },
  ];

  return (
    <Container header={<Header variant="h3">Sample questions</Header>}>
      <SpaceBetween size="m">
        {questionCategories.map((category, index) => (
          <ExpandableSection
            key={index}
            headerText={category.title}
            variant="default"
            defaultExpanded={category.defaultExpanded}
          >
            <SpaceBetween size="xs">
              {category.questions.map((question, qIndex) => (
                <Link 
                  key={qIndex} 
                  variant="primary"
                  onFollow={() => onQuestionClick(question)}
                >
                  {question}
                </Link>
              ))}
            </SpaceBetween>
          </ExpandableSection>
        ))}
      </SpaceBetween>
    </Container>
  );
}
