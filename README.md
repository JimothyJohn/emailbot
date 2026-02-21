# Emailbot: AWS Serverless Email Assistant

Emailbot is a fully serverless, AI-powered email assistant designed to act as a 24/7 intelligent auto-responder. It routes inbound emails straight into a managed LLM (Google Gemini 2.5 Flash), generates context-aware responses, and gracefully sends elegantly styled HTML replies back to the user. 

It runs entirely within AWS free-tier native infrastructure, utilizing minimal compute overhead while remaining highly robust.

## Architecture
- **Inbound:** Route53 handles MX records, pointing `@advin.io` directly into Amazon SES.
- **Ingestion:** SES drops incoming raw raw `.eml` files into a locked-down **Amazon S3** bucket.
- **Compute:** S3 triggers an **AWS Lambda** execution, which decompresses nested `multipart` email bodies.
- **State Management:** **Amazon DynamoDB** acts as an ultra-fast caching layer for idempotency, ensuring the same `Message-ID` never gets processed twice (preventing infinite email loops).
  - **Reasoning:** The Lambda function invokes the `gemini-3.1-pro` model, injecting context like `PERSONALITY.md` and enabling internet search tools (`google-search`) for live data lookups.
- **Outbound:** The raw Markdown response is compiled into beautiful HTML and dispatched back to the original sender via **Amazon SES**.

## Configuration
The bot's entire persona, tone, and formatting instructions are controlled completely via the `PERSONALITY.md` file located at the project root. You do not need to modify the Python execution logic to adjust the AI's behavior. Simply update the Markdown file and redeploy.

## Requirements
- `uv` - The blazingly fast Python package manager.
- `aws-sam-cli` - AWS Serverless Application Model deployment toolkit.
- An active AWS Account configured securely with IAM.

## Deployment
1. Initialize the virtual environment and sync constraints:
   ```bash
   uv sync
   ```
2. Run the isolated TDD Unit Test suite to ensure all AWS dependencies (`moto`) and error handlers are green:
   ```bash
   uv run pytest -m unit
   ```
3. Bundle `PERSONALITY.md`, export frozen requirements, and deploy the entire CloudFormation artifact stack to AWS:
   ```bash
   ./Quickstart -p
   ```
