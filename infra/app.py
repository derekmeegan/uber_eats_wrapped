import os
from aws_cdk import App, Environment
from stack import UberEatsAnalyzerStack

app = App()

# Define the AWS environment
env = Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

stack = UberEatsAnalyzerStack(app, "UberEatsAnalyzerStack", env=env)

app.synth()