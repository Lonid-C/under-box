"""One job per subprocess. No credentials, input text or provider errors in logs."""
import contextlib
import ipaddress
import json
import os
import socket
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'open_box'))
# Block private/local/metadata egress including redirect targets in this worker.
_original_resolve = socket.getaddrinfo
_original_connect = socket.socket.connect

def _public(address):
    ip = ipaddress.ip_address(address.split('%')[0])
    return ip.is_global and not (getattr(ip, 'ipv4_mapped', None) and not ip.ipv4_mapped.is_global)
def resolve(host, port, *a, **kw):
    if port not in (80, 443, '80', '443', 'http', 'https'):
        raise OSError('network_destination_blocked')
    answers = _original_resolve(host, port, *a, **kw)
    if any(not _public(x[4][0]) for x in answers):
        raise OSError('network_destination_blocked')
    return answers
def connect(sock, address):
    if isinstance(address, tuple) and (not _public(address[0]) or address[1] not in (80,443)):
        raise OSError('network_destination_blocked')
    return _original_connect(sock, address)
socket.getaddrinfo = resolve
socket.socket.connect = connect

def main():
    from live_verify import verify_resume_text, LiveVerificationUnavailable
    from app.llm import LLMUnavailable, LLMTransient
    from app.search import SearchUnavailable
    request = json.loads(sys.stdin.read())
    def progress(*a, **kw):
        # Stage codes only. Never echo the pipeline's resume-specific messages.
        pass
    try:
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = verify_resume_text(request['resume_text'], request['consent'], progress=progress)
        response = {'state': 'completed', 'result': result}
        if len(json.dumps(response,ensure_ascii=False).encode()) > 120000:
            response = {'state':'failed','error':'report_too_large'}
    except (LLMUnavailable, LLMTransient, SearchUnavailable):
        response = {'state':'failed','error':'provider_unavailable'}
    except LiveVerificationUnavailable as exc:
        response = {'state': 'failed', 'error': str(exc)}
    except ValueError as exc:
        allowed = {'no_verifiable_claims','too_many_claims_max_8','consent_required','empty_resume','invalid_resume_size'}
        response = {'state':'failed','error':str(exc) if str(exc) in allowed else 'invalid_resume_or_model_output'}
    except Exception:
        response = {'state':'failed','error':'verification_failed'}
    print(json.dumps(response, ensure_ascii=False))
if __name__ == '__main__':
    main()
