import os
import pytest
import boto3
from moto import mock_aws

@pytest.fixture(scope="function")
def aws_credentials():
    """Mocked AWS Credentials for moto."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["DYNAMODB_TABLE"] = "test-idempotency-table"
    os.environ["S3_BUCKET"] = "test-raw-email-bucket"
    os.environ["GEMINI_API_KEY_PARAM_NAME"] = "/emailbot/dev/gemini-api-key"
    os.environ["POWERTOOLS_SERVICE_NAME"] = "EmailAssistantTest"
    os.environ["LOG_LEVEL"] = "DEBUG"

@pytest.fixture(scope="function")
def s3_client(aws_credentials):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=os.environ["S3_BUCKET"])
        yield client

@pytest.fixture(scope="function")
def dynamodb_client(aws_credentials):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=os.environ["DYNAMODB_TABLE"],
            KeySchema=[{"AttributeName": "message_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "message_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST"
        )
        yield client

@pytest.fixture(scope="function")
def ssm_client(aws_credentials):
    with mock_aws():
        client = boto3.client("ssm", region_name="us-east-1")
        client.put_parameter(
            Name="/emailbot/dev/gemini-api-key",
            Description="Gemini API Key",
            Value="AIzaSyMockKeyForGemini12345",
            Type="SecureString"
        )
        yield client

@pytest.fixture(scope="function")
def ses_client(aws_credentials):
    with mock_aws():
        client = boto3.client("ses", region_name="us-east-1")
        client.verify_email_identity(EmailAddress="noreply@example.com")
        yield client
