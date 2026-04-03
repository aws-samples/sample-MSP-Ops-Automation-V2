import React from 'react'
import ReactDOM from 'react-dom/client'
import '@cloudscape-design/global-styles/index.css'
import App from './App'

/**
 * Top-level error boundary.
 *
 * Catches any unhandled React render errors beneath it and replaces the
 * blank/broken screen with a minimal fallback UI that shows the error
 * message and a reload button. This prevents silent white-screens in
 * production while still surfacing enough detail for debugging.
 *
 * Intentionally kept simple — no external logging SDK — because this fires
 * only for catastrophic failures that escape all component-level handling.
 */
// Add error boundary
class ErrorBoundary extends React.Component<{children: React.ReactNode}, {hasError: boolean, error: any}> {
  constructor(props: {children: React.ReactNode}) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: any) {
    return { hasError: true, error };
  }

  componentDidCatch(error: any, errorInfo: any) {
    console.error('React Error:', error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: '20px', fontFamily: 'sans-serif' }}>
          <h1>Something went wrong</h1>
          <pre style={{ background: '#f5f5f5', padding: '10px', overflow: 'auto' }}>
            {this.state.error?.toString()}
          </pre>
          <button onClick={() => window.location.reload()}>Reload</button>
        </div>
      );
    }

    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
