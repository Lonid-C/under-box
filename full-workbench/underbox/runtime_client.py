"""Fixed-destination AgentArts IAM client. Never log signed headers or payloads."""
import json
import os
import urllib.request
import uuid
from apig_sdk import signer

ENDPOINT = 'https://defaultgw-gnrdembxhe.cn-southwest-2.huaweicloud-agentarts.com/runtimes/underbox-ict-demo/invocations'

def configured():
    return bool(os.environ.get('HUAWEICLOUD_SDK_AK') and os.environ.get('HUAWEICLOUD_SDK_SK'))

def invoke(query, session_id=None):
    if not configured():
        raise RuntimeError('iam_not_configured')
    headers={'Content-Type':'application/json', 'Accept':'text/event-stream',
             'X-Sdk-Content-Sha256':'UNSIGNED-PAYLOAD', 'X-Invoke-Mode':'published',
             'x-hw-agentarts-session-id':session_id or uuid.uuid4().hex}
    body=json.dumps({'query':query},ensure_ascii=False)
    sig=signer.Signer(algorithm='V11-HMAC-SHA256',region_id='cn-southwest-2')
    sig.Key=os.environ['HUAWEICLOUD_SDK_AK']; sig.Secret=os.environ['HUAWEICLOUD_SDK_SK']
    req=signer.HttpRequest('POST',ENDPOINT,headers,body); sig.Sign(req)
    request=urllib.request.Request(ENDPOINT,data=req.body,headers=req.headers,method='POST')
    return urllib.request.urlopen(request,timeout=480)
