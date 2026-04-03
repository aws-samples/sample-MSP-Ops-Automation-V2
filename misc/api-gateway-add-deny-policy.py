import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from strands import Agent
from strands_tools import use_aws

# Load environment variables from .env file
load_dotenv()

# Get configuration from environment variables
AWS_REGION = 'us-east-1'
API_ID = os.getenv('API_ID', '')

if not API_ID:
    print("⚠️ Warning: API_ID not set in environment. Set it in .env file or as environment variable.")

os.environ['BYPASS_TOOL_CONSENT'] = 'true'

agent = Agent(
    tools=[use_aws]
)

def get_user_choice():
    while True:
        print("\n=== API Gateway Policy Manager ===")
        print("1. Add DENY policy (block all access)")
        print("2. Add ALLOW policy (restore access)")
        print("3. Exit application")
        
        choice = input("\nEnter your choice (1-3): ").strip()
        
        if choice in ['1', '2', '3']:
            return choice
        else:
            print("Invalid choice. Please enter 1, 2, or 3.")

def apply_policy(policy_type):
    import json
    import time
    
    if policy_type == "deny":
        policy_dict = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "*",
                    "Resource": f"arn:aws:execute-api:{AWS_REGION}:*:{API_ID}/prod/*"
                }
            ]
        }
        print("=== Adding DENY policy to API Gateway ===")
    else:  # allow
        policy_dict = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "*",
                    "Resource": f"arn:aws:execute-api:{AWS_REGION}:*:{API_ID}/prod/*"
                }
            ]
        }
        print("=== Adding ALLOW policy to API Gateway ===")

    # Convert policy to stringified JSON
    policy_string = json.dumps(policy_dict)
    print(f"Policy to apply: {policy_string}")

    # Apply policy using REPLACE operation
    print("\n=== Applying policy ===")
    result = agent.tool.use_aws(
        service_name="apigateway",
        operation_name="update_rest_api",
        parameters={
            "restApiId": API_ID,
            "patchOperations": [
                {
                    "op": "replace",
                    "path": "/policy",
                    "value": policy_string
                }
            ]
        },
        region=AWS_REGION,
        label=f"Replace with {policy_type} policy"
    )
    
    if result.get('status') == 'success':
        print("✅ Policy update successful")
    else:
        print(f"❌ Policy update failed: {result}")
        return

    # Wait for policy to propagate
    print("\n=== Waiting for policy to propagate ===")
    time.sleep(3)  # nosemgrep: arbitrary-sleep - Required for AWS API Gateway policy propagation delay

    # Force redeploy
    print("=== Redeploying API Gateway ===")
    deploy_result = agent.tool.use_aws(
        service_name="apigateway",
        operation_name="create_deployment",
        parameters={
            "restApiId": API_ID,
            "stageName": "prod",
            "description": f"Policy update - {policy_type}"
        },
        region=AWS_REGION,
        label="Redeploy API Gateway"
    )
    
    if deploy_result.get('status') == 'success':
        print("✅ Deployment successful")
    else:
        print(f"❌ Deployment failed: {deploy_result}")
        return

    # Wait for deployment to complete
    time.sleep(2)  # nosemgrep: arbitrary-sleep - Required for AWS API Gateway deployment completion

    # Check final policy
    print("\n=== Verifying final policy ===")
    final_result = agent.tool.use_aws(
        service_name="apigateway",
        operation_name="get_rest_api",
        parameters={
            "restApiId": API_ID
        },
        region=AWS_REGION,
        label="Get final API policy"
    )
    
    if final_result.get('status') == 'success':
        # Extract and parse the policy from the response
        content = final_result.get('content', [{}])[0].get('text', '')
        if 'policy' in content:
            import re
            policy_match = re.search(r"'policy': '([^']+)'", content)
            if policy_match:
                policy_json = policy_match.group(1).replace('\\"', '"').replace('\\/', '/')
                try:
                    parsed_policy = json.loads(policy_json)
                    effect = parsed_policy['Statement'][0]['Effect']
                    print(f"✅ Final policy verified - Effect: {effect}")
                    print(f"Policy: {json.dumps(parsed_policy, indent=2)}")
                except:
                    print(f"Policy string: {policy_json}")
            else:
                print("Could not extract policy from response")
        else:
            print("No policy found in response")
    
    print(f"\n🎉 API Gateway successfully updated with {policy_type.upper()} policy and redeployed!")

def main():
    while True:
        choice = get_user_choice()
        
        if choice == '1':
            apply_policy("deny")
        elif choice == '2':
            apply_policy("allow")
        elif choice == '3':
            print("Exiting application...")
            break

if __name__ == "__main__":
    main()
