from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Any

class S3Object(BaseModel):
    key: str
    size: int
    eTag: str

class S3Bucket(BaseModel):
    name: str
    arn: str

class S3EventRecordDetail(BaseModel):
    s3SchemaVersion: Optional[str] = None
    configurationId: Optional[str] = None
    bucket: S3Bucket
    object: S3Object

class S3EventRecord(BaseModel):
    eventVersion: Optional[str] = None
    eventSource: Optional[str] = None
    awsRegion: Optional[str] = None
    eventTime: Optional[str] = None
    eventName: Optional[str] = None
    s3: S3EventRecordDetail

class S3EventBody(BaseModel):
    Records: List[S3EventRecord]

class ParsedEmail(BaseModel):
    message_id: str
    sender: str
    subject: str
    text_body: str
    html_body: Optional[str] = None
    recipient: str
