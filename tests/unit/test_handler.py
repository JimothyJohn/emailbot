import pytest
import os
import json
import boto3
from email.message import EmailMessage
from datetime import datetime, timezone
from assistant.app import lambda_handler, get_gemini_client, generate_llm_response, send_ses_email
from botocore.exceptions import ClientError
from google import genai

pytestmark = pytest.mark.unit

def create_mock_s3_event(bucket_name: str, object_key: str) -> dict:
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "awsRegion": "us-east-1",
                "s3": {
                    "bucket": {"name": bucket_name, "arn": f"arn:aws:s3:::{bucket_name}"},
                    "object": {"key": object_key, "size": 1024, "eTag": "test-etag"}
                }
            }
        ]
    }

def create_raw_email(sender: str, recipient: str, message_id: str, subject: str = "Test") -> bytes:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = recipient
    msg['Message-ID'] = f"<{message_id}>"
    msg.set_content("This is a test email body.")
    return msg.as_bytes()

def test_handler_idempotency_prevents_duplicate_processing(s3_client, dynamodb_client, ses_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    table = os.environ["DYNAMODB_TABLE"]
    msg_id = "duplicate-msg-123"
    
    # Pre-populate DynamoDB with the message ID
    dynamodb_client.put_item(
        TableName=table,
        Item={"message_id": {"S": msg_id}, "expires_at": {"N": "9999999999"}}
    )
    
    # Put an email in S3 with this message ID
    raw_email = create_raw_email("user@example.com", "bot@advin.io", msg_id)
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=raw_email)
    
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    # Spy on SES to ensure it is NOT called
    mock_ses = mocker.patch("boto3.client")
    
    response = lambda_handler(event, mocker.MagicMock())
    
    assert response["statusCode"] == 200
    assert response["body"] == "Ignored: Already processed"
    
    # Ensure SES was never called
    mock_ses.return_value.send_email.assert_not_called()

def test_handler_ignores_noreply_addresses(s3_client, dynamodb_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    msg_id = "noreply-msg-456"
    
    raw_email = create_raw_email("noreply@spammer.com", "bot@advin.io", msg_id)
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=raw_email)
    
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    response = lambda_handler(event, mocker.MagicMock())
    
    assert response["statusCode"] == 200
    assert response["body"] == "Ignored: Automated sender"

def test_handler_processes_valid_email_successfully(s3_client, dynamodb_client, ses_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    table = os.environ["DYNAMODB_TABLE"]
    msg_id = "valid-msg-789"
    
    raw_email = create_raw_email("real.user@example.com", "bot@advin.io", msg_id, "Help me")
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=raw_email)
    
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    # Mock the LLM solver and SES outbox
    mocker.patch("assistant.app.generate_llm_response", return_value="Here is your answer.")
    mock_ses = mocker.patch("assistant.app.send_ses_email")
    
    response = lambda_handler(event, mocker.MagicMock())
    
    assert response["statusCode"] == 200
    assert response["body"] == "Successfully processed"
    
    # Verify Idempotency record was created
    db_response = dynamodb_client.get_item(
        TableName=table,
        Key={"message_id": {"S": msg_id}}
    )
    assert "Item" in db_response
    
    # Verify SES sent an email
    mock_ses.assert_called_once()

def test_handler_ignores_unauthorized_recipients(s3_client, dynamodb_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    msg_id = "unauth-recipient-msg-123"
    
    # Recipient is NOT bot@advin.io
    raw_email = create_raw_email("user@example.com", "random@advin.io", msg_id)
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=raw_email)
    
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    response = lambda_handler(event, mocker.MagicMock())
    
    assert response["statusCode"] == 200
    assert response["body"] == "Ignored: Unauthorized recipient"

def test_handler_invalid_s3_event_returns_400(mocker):
    response = lambda_handler({"bad": "data"}, mocker.MagicMock())
    assert response["statusCode"] == 400
    assert response["body"] == "Invalid event"

def test_handler_missing_env_vars_returns_500(mocker, monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("DYNAMODB_TABLE", raising=False)
    event = create_mock_s3_event("test-bucket", "test-key")
    response = lambda_handler(event, mocker.MagicMock())
    assert response["statusCode"] == 500
    assert response["body"] == "Configuration error"

def test_handler_s3_get_object_failure_ignored(s3_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    msg_id = "s3-fail-msg-123"
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    import assistant.app
    mocker.patch.object(assistant.app.s3_client, "get_object", side_effect=ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject"))
    
    response = lambda_handler(event, mocker.MagicMock())
    assert response["statusCode"] == 200

def test_handler_dynamodb_put_item_failure_succeeds(s3_client, dynamodb_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    msg_id = "dynamo-fail-msg-123"
    raw_email = create_raw_email("user@example.com", "bot@advin.io", msg_id)
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=raw_email)
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    mocker.patch("assistant.app.generate_llm_response", return_value="Here is your answer.")
    mocker.patch("assistant.app.send_ses_email")
    
    import assistant.app
    mocker.patch.object(assistant.app.dynamodb_client, "put_item", side_effect=ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}}, "PutItem"))
    
    response = lambda_handler(event, mocker.MagicMock())
    assert response["statusCode"] == 200

def test_handler_multipart_email_parsing(s3_client, dynamodb_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    msg_id = "multipart-msg-123"
    
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    
    msg = MIMEMultipart("alternative")
    msg['Subject'] = "Multipart test"
    msg['From'] = "user@example.com"
    msg['To'] = "bot@advin.io"
    msg['Message-ID'] = f"<{msg_id}>"
    msg.attach(MIMEText("This is the plain text part.", "plain"))
    msg.attach(MIMEText("<html><body>This is the HTML part.</body></html>", "html"))
    
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=msg.as_bytes())
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    mock_llm = mocker.patch("assistant.app.generate_llm_response", return_value="Reply.")
    mocker.patch("assistant.app.send_ses_email")
    
    response = lambda_handler(event, mocker.MagicMock())
    assert response["statusCode"] == 200
    mock_llm.assert_called_once()
    assert mock_llm.call_args[0][0] == "This is the plain text part."

def test_get_gemini_client_success(ssm_client):
    import assistant.app
    assistant.app._gemini_client = None
    client = get_gemini_client()
    assert client is not None
    assert isinstance(client, genai.Client)

def test_get_gemini_client_missing_env(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_PARAM_NAME", raising=False)
    import assistant.app
    assistant.app._gemini_client = None
    with pytest.raises(ValueError):
        get_gemini_client()

def test_get_gemini_client_ssm_failure(mocker, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY_PARAM_NAME", "/mock/key")
    import assistant.app
    assistant.app._gemini_client = None
    mocker.patch.object(assistant.app.ssm_client, "get_parameter", side_effect=ClientError({"Error": {"Code": "DecryptionFailure", "Message": "Decryption failed"}}, "GetParameter"))
    with pytest.raises(ClientError):
        get_gemini_client()

def test_get_personality_success(mocker):
    from assistant.app import get_personality
    mock_open = mocker.mock_open(read_data="Mocked Persona")
    mocker.patch("builtins.open", mock_open)
    res = get_personality()
    assert res == "Mocked Persona"

def test_get_personality_fallback(mocker):
    from assistant.app import get_personality
    mocker.patch("builtins.open", side_effect=FileNotFoundError)
    res = get_personality()
    assert res == "You are a helpful email assistant named Botslave."

def test_generate_llm_response_success(mocker):
    mock_client = mocker.MagicMock()
    mock_client.models.generate_content.return_value.text = "Generated Reply"
    mocker.patch("assistant.app.get_gemini_client", return_value=mock_client)
    res = generate_llm_response("Test body")
    assert res == "Generated Reply"
    
def test_generate_llm_response_failure(mocker):
    mock_client = mocker.MagicMock()
    mock_client.models.generate_content.side_effect = Exception("API Down")
    mocker.patch("assistant.app.get_gemini_client", return_value=mock_client)
    res = generate_llm_response("Test body")
    assert res == "I'm sorry, I am currently unable to process your request."

def test_send_ses_email_success(ses_client):
    ses_client.verify_email_identity(EmailAddress="from@advin.io")
    send_ses_email("to@example.com", "from@advin.io", "Subj", "Body text", "<b>Body html</b>", "123")
    
def test_send_ses_email_failure(mocker):
    import assistant.app
    mocker.patch.object(assistant.app.ses_client, "send_email", side_effect=ClientError({"Error": {"Code": "MessageRejected", "Message": "Rejected"}}, "SendEmail"))
    with pytest.raises(ClientError):
        send_ses_email("to@example.com", "from@advin.io", "Subj", "Body text", "<b>Body html</b>", "123")

def test_handler_dynamodb_get_item_failure_skips_record(s3_client, dynamodb_client, mocker):
    bucket = os.environ["S3_BUCKET"]
    msg_id = "dynamo-get-fail-msg-123"
    raw_email = create_raw_email("user@example.com", "bot@advin.io", msg_id)
    s3_client.put_object(Bucket=bucket, Key=f"emails/{msg_id}", Body=raw_email)
    event = create_mock_s3_event(bucket, f"emails/{msg_id}")
    
    import assistant.app
    mocker.patch.object(assistant.app.dynamodb_client, "get_item", side_effect=ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}}, "GetItem"))
    
    mock_llm = mocker.patch("assistant.app.generate_llm_response")
    
    response = lambda_handler(event, mocker.MagicMock())
    assert response["statusCode"] == 200
    mock_llm.assert_not_called()

def test_render_html_success(mocker):
    from assistant.app import render_html
    mock_open = mocker.mock_open(read_data="WRAPPER {{CONTENT}} WRAPPER")
    mocker.patch("builtins.open", mock_open)
    res = render_html("**bold**")
    assert res == "WRAPPER <p><strong>bold</strong></p> WRAPPER"

def test_render_html_fallback(mocker):
    from assistant.app import render_html
    mocker.patch("builtins.open", side_effect=FileNotFoundError)
    res = render_html("**bold**")
    assert res == "<html><body><p><strong>bold</strong></p></body></html>"
