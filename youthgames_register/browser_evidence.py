"""Small browser network ledger; no cookies, tokens, query strings or POST bodies."""

import json
from pathlib import Path
import time
from urllib.parse import urlparse

RATE_HEADERS = {'retry-after','ratelimit','ratelimit-policy','ratelimit-limit','ratelimit-remaining',
                'ratelimit-reset','x-ratelimit-limit','x-ratelimit-remaining','x-ratelimit-reset'}


class PageEvidence:
    def __init__(self, page, emit, metadata=None):
        self.page, self.emit, self.metadata = page, emit, metadata or {}
        self.records = []
        self.started = {}
        self.listeners = [('request',self.request),('response',self.response),('requestfailed',self.failed)]
        for event, callback in self.listeners:
            page.on(event,callback)

    def relevant(self, request):
        parsed = urlparse(request.url)
        return (parsed.scheme=='https' and parsed.netloc=='youthgames.punjab.gov.pk'
                and request.resource_type=='document' and request.frame==self.page.main_frame)

    def request(self, request):
        if self.relevant(request):
            self.started[id(request)] = time.monotonic()

    def response(self, response):
        request = response.request
        if not self.relevant(request):return
        start = self.started.pop(id(request),None)
        record = {'event':'response','method':request.method,'path':urlparse(request.url).path or '/',
                  'status':response.status,'rate_headers':{k:v[:500] for k,v in response.headers.items() if k.lower() in RATE_HEADERS},
                  'seconds':round(time.monotonic()-start,3) if start is not None else None}
        self.records.append(record)
        self.emit('browser_response',**self.metadata,**record)

    def failed(self, request):
        if not self.relevant(request):return
        self.started.pop(id(request),None)
        record = {'event':'request_failed','method':request.method,'path':urlparse(request.url).path or '/'}
        self.records.append(record)
        self.emit('browser_request_failed',**self.metadata,**record)

    def close(self, folder=None):
        for event,callback in self.listeners:
            self.page.remove_listener(event,callback)
        self.listeners = []
        if folder is not None:
            Path(folder,'browser-network.json').write_text(json.dumps({'transport':'playwright','events':self.records},indent=2)+'\n')
