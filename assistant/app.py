import os
import time
import boto3
import email
from email.message import EmailMessage
from typing import Any, Dict, Optional
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from google import genai
from google.genai import types
import markdown
from assistant.models import S3EventBody, ParsedEmail

logger = Logger()

# Initialize AWS clients outside the handler for connection reuse
s3_client = boto3.client("s3")
dynamodb_client = boto3.client("dynamodb")
ses_client = boto3.client("ses")
ssm_client = boto3.client("ssm")

# Cache the Gemini client outside the handler
_gemini_client: Optional[genai.Client] = None

def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        param_name = os.environ.get("GEMINI_API_KEY_PARAM_NAME", "")
        if not param_name:
            raise ValueError("GEMINI_API_KEY_PARAM_NAME environment variable is required")
        
        try:
            logger.info("Fetching Gemini API key from SSM.")
            response = ssm_client.get_parameter(
                Name=param_name,
                WithDecryption=True
            )
            api_key = response["Parameter"]["Value"]
            _gemini_client = genai.Client(api_key=api_key)
        except ClientError as e:
            logger.error(f"Failed to retrieve API key from SSM: {e}")
            raise
    return _gemini_client

def get_personality() -> str:
    """Reads the PERSONALITY.md file from the bundled Lambda package."""
    try:
        # Resolve path relative to this script's location (assistant/app.py -> assistant/PERSONALITY.md)
        personality_path = os.path.join(os.path.dirname(__file__), "PERSONALITY.md")
        with open(personality_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("PERSONALITY.md not found. Defaulting to standard assistant persona.")
        return "You are a helpful email assistant named Botslave."

def render_html(markdown_text: str) -> str:
    """Converts LLM markdown into elegantly styled HTML using template.html."""
    try:
        html_content = markdown.markdown(markdown_text, extensions=['tables', 'fenced_code'])
        
        template_path = os.path.join(os.path.dirname(__file__), "template.html")
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
            
        return template.replace("{{CONTENT}}", html_content)
    except Exception as e:
        logger.error(f"Failed to render HTML: {e}")
        # Fallback to a bare-bones HTML wrapper if the template is missing
        return f"<html><body>{markdown.markdown(markdown_text)}</body></html>"

def generate_llm_response(text_body: str) -> str:
    """Generate a response using Google's Gemini."""
    client = get_gemini_client()
    system_instruction = get_personality()
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Respond to this email:\n\n{text_body}",
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[{"google_search": {}}],
            )
        )
        return str(response.text)
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return "I'm sorry, I am currently unable to process your request."

def send_ses_email(to_address: str, from_address: str, subject: str, text_body: str, html_body: str, in_reply_to: str) -> None:
    # Actually just simple send_email for SES
    try:
        ses_client.send_email(
            Source=from_address,
            Destination={"ToAddresses": [to_address]},
            Message={
                "Subject": {"Data": f"Re: {subject}"},
                "Body": {
                    "Text": {"Data": text_body},
                    "Html": {"Data": html_body}
                }
            }
        )
        logger.info(f"Email sent to {to_address}")
    except ClientError as e:
        logger.error(f"SES Error: {e.response['Error']['Message']}")
        raise

@logger.inject_lambda_context(log_event=True)
def lambda_handler(event: Dict[str, Any], context: LambdaContext) -> Dict[str, Any]:
    """
    Main entry point for processing an S3 email event.
    """
    logger.info("Function invoked.")
    
    try:
        s3_event = S3EventBody(**event)
    except Exception as e:
        logger.error(f"Invalid S3 Event Format: {e}")
        return {"statusCode": 400, "body": "Invalid event"}

    s3_bucket = os.environ.get("S3_BUCKET")
    dynamo_table = os.environ.get("DYNAMODB_TABLE")
    
    if not s3_bucket or not dynamo_table:
        logger.error("Missing required environment variables.")
        return {"statusCode": 500, "body": "Configuration error"}

    for record in s3_event.Records:
        bucket_name = record.s3.bucket.name
        object_key = record.s3.object.key
        
        try:
            s3_response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
            raw_bytes = s3_response['Body'].read()
        except ClientError as e:
            logger.error(f"Failed to fetch {object_key} from {bucket_name}: {e}")
            continue

        msg = email.message_from_bytes(raw_bytes)
        sender = str(msg.get("From", ""))
        message_id = str(msg.get("Message-ID", "")).strip("<>")
        subject = str(msg.get("Subject", ""))
        
        # Simple extraction for text body
        text_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    text_body = payload.decode(errors="ignore") if payload else ""
                    break
        else:
            payload = msg.get_payload(decode=True)
            text_body = payload.decode(errors="ignore") if hasattr(payload, "decode") else str(payload)

        parsed_email = ParsedEmail(
            message_id=message_id,
            sender=sender,
            subject=subject,
            text_body=text_body,
            recipient=str(msg.get("To", ""))
        )
        
        # 1. Check Idempotency
        try:
            db_res = dynamodb_client.get_item(
                TableName=dynamo_table,
                Key={"message_id": {"S": message_id}},
                ConsistentRead=True
            )
            if "Item" in db_res:
                logger.info(f"Duplicate Message-ID ignored: {message_id}")
                return {"statusCode": 200, "body": "Ignored: Already processed"}
        except ClientError as e:
            logger.error(f"DynamoDB Error during get_item: {e}")
            continue
            
        # 2. Check for automated loops
        sender_lower = sender.lower()
        if "noreply" in sender_lower or "daemon" in sender_lower or "bot@" in sender_lower:
            logger.info(f"Ignored automated sender: {sender}")
            return {"statusCode": 200, "body": "Ignored: Automated sender"}
            
        # 3. Check for unauthorized recipients
        recipient_lower = parsed_email.recipient.lower()
        if "bot@advin.io" not in recipient_lower:
            logger.info(f"Ignored unauthorized recipient: {parsed_email.recipient}")
            return {"statusCode": 200, "body": "Ignored: Unauthorized recipient"}
            
        # 4. Generate LLM Action (mocked)
        reply_md = generate_llm_response(parsed_email.text_body)
        reply_html = render_html(reply_md)
        
        # 4. Action: Send Email
        bot_address = parsed_email.recipient if parsed_email.recipient and "@" in parsed_email.recipient else "bot@advin.io"
        send_ses_email(to_address=sender, from_address=bot_address, subject=subject, text_body=reply_md, html_body=reply_html, in_reply_to=message_id)
        
        # 5. Save Idempotency
        expires_at = int(time.time()) + (30 * 24 * 60 * 60) # 30 days
        try:
            dynamodb_client.put_item(
                TableName=dynamo_table,
                Item={
                    "message_id": {"S": message_id},
                    "expires_at": {"N": str(expires_at)}
                }
            )
        except ClientError as e:
            logger.error(f"Failed to write to DynamoDB for idempotency: {e}")
            # Still returning success since action took place

    return {"statusCode": 200, "body": "Successfully processed"}
