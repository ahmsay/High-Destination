"""Entry point of the web API on AWS Lambda (API Gateway HTTP API -> Mangum -> FastAPI).

The function's environment carries the stack's names (HD_BUCKET, HD_QUEUE_URL,
HD_ASG) and the Cognito app client id (HD_CLIENT_ID). Login is checked twice:
API Gateway's JWT authorizer rejects bad tokens, and auth.py checks the claims.
"""
import os

from mangum import Mangum

from . import core, web

app = web.create_app(core.Stack.from_env(), auth_mode="cognito", client_id=os.environ["HD_CLIENT_ID"])
handler = Mangum(app, lifespan="off")
