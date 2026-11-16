"""Report-scoped conservative CNY estimates, reserved before paid requests.

Server usage is settled from returned tokens when available. Unknown/failed
requests retain their reservation. AgentArts uses a separately labelled estimate;
this is an application budget, not a provider billing guarantee.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json,math,os,re,secrets,threading,time
from pathlib import Path

CURRENT = ContextVar('underbox_report_cost_budget', default=None)
SEARCH_PRICES = {'search_std': .01, 'search_pro': .03, 'search_pro_sogou': .05, 'search_pro_quark': .05}


class BudgetLimit(ValueError):
    pass


class CostBudget:
    def __init__(self, limit=4.0, target=3.0, cloud_allowance=.5, *, state=None):
        self._lock = threading.RLock()
        self.id = secrets.token_hex(16)
        self.limit, self.target = float(limit), float(target)
        self.spent = float(cloud_allowance)
        self.cloud_allowance = float(cloud_allowance)
        self.categories = {'agentarts_allowance': self.cloud_allowance}
        self.search_calls = 0
        self.search_limit = 1000  # Emergency brake; CNY is the controlling budget.
        self.stopped = False
        self.reason = ''
        self.created = time.time()
        if state:
            for key in ('id','limit','target','spent','cloud_allowance','categories','search_calls','search_limit','stopped','reason','created'):
                setattr(self, key, state[key])
        if not re.fullmatch('[0-9a-f]{32}', self.id):raise ValueError('invalid_budget_id')
        if not (0 <= self.spent and 0 < self.target <= self.limit <= 4):raise ValueError('invalid_budget_limits')
        self._persist()

    @staticmethod
    def path(ident):
        db = os.environ.get('UNDERBOX_DB')
        if not db:return None
        folder = Path(db).parent/'cost-budgets';folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        return folder/(ident+'.json')

    @classmethod
    def load(cls, ident):
        if not re.fullmatch('[0-9a-f]{32}', ident):raise ValueError('invalid_budget_id')
        path = cls.path(ident)
        if path is None:raise ValueError('missing_budget_storage')
        return cls(state=json.loads(path.read_text()))

    def _persist(self):
        path = self.path(self.id)
        if path is None:return
        keys = ('id','limit','target','spent','cloud_allowance','categories','search_calls','search_limit','stopped','reason','created')
        temp = path.with_suffix('.'+secrets.token_hex(8)+'.tmp')
        try:
            fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({key:getattr(self,key) for key in keys}, stream)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temp,path)
        finally:temp.unlink(missing_ok=True)

    def reserve(self, amount, category, *, search=False):
        if not math.isfinite(amount) or amount < 0:
            self.stop('无法确认该接口的计费价格，已停止新增请求。')
            raise BudgetLimit(self.reason)
        with self._lock:
            if (self.stopped or self.spent+amount>self.limit+1e-9
                    or search and self.search_calls>=self.search_limit):
                self.stopped=True
                self.reason=('达到整份简历的搜索安全上限，已停止新增搜索。' if search and self.search_calls>=self.search_limit
                             else '估算费用预算不足，已停止新增付费请求；已有证据仍保留。')
                self._persist()
                raise BudgetLimit(self.reason)
            if search:self.search_calls+=1
            self.spent += amount
            self.categories[category] = self.categories.get(category,0)+amount
            self._persist()  # Durable reservation precedes network I/O.
        return amount,category

    def stop(self,reason):
        with self._lock:
            self.stopped=True;self.reason=reason;self._persist()

    def settle(self, reservation, actual):
        if actual is None or not math.isfinite(actual) or actual < 0:return
        amount,category = reservation
        with self._lock:
            self.spent = max(0,self.spent-amount+actual)
            self.categories[category] = max(0,self.categories.get(category,0)-amount+actual)
            if self.spent>self.limit:self.stopped=True;self.reason='供应商用量超过预估，已停止新增付费请求。'
            self._persist()

    def snapshot(self):
        with self._lock:
            return {'limitCny':self.limit,'targetCny':self.target,'estimatedCny':round(self.spent,4),
                    'remainingCny':round(max(0,self.limit-self.spent),4),'stopped':self.stopped,'reason':self.reason,
                    'cloudAllowanceCny':self.cloud_allowance,'cloudEstimateOnly':True,
                    'searchRequests':self.search_calls,'searchSafetyLimit':self.search_limit,
                    'byCategory':{key:round(value,4) for key,value in self.categories.items()},
                    'notice':'按公开单价保守估算；含华为云预留，实际费用以各平台账单为准。问答和语音另计。'}


@contextmanager
def bind(budget):
    token=CURRENT.set(budget)
    try:yield budget
    finally:CURRENT.reset(token)


def reserve(amount,category,*,search=False):
    budget=CURRENT.get()
    return (budget,budget.reserve(amount,category,search=search)) if budget else None


def settle(ticket,actual):
    if ticket:ticket[0].settle(ticket[1],actual)


def search_allowed():
    budget=CURRENT.get()
    return not budget or not budget.stopped and budget.search_calls<budget.search_limit


def alternate_allowed():
    budget=CURRENT.get()
    return not budget or not budget.stopped and budget.spent<budget.target


def model_prices(provider,model):
    if provider=='deepseek' and model in ('deepseek-flash','deepseek-v4-flash','deepseek-v4-flash-vision-exp'):
        return 2.,.04,8.  # Peak CNY/M tokens; conservative even off peak.
    if provider=='zhipu' and model in ('glm-4.7-flash','glm-4-flash-250414'):
        return 0.,0.,0.
    values=[]
    prefix='DEEPSEEK' if provider=='deepseek' else 'ZHIPU'
    for suffix in ('IN','IN_CACHED','OUT'):
        raw=os.environ.get(prefix+'_PRICE_'+suffix)
        if raw is None and suffix=='IN_CACHED':raw=os.environ.get(prefix+'_PRICE_IN')
        try:values.append(float(raw))
        except (ValueError,TypeError):raise BudgetLimit('当前模型未配置可核对单价，已停止付费调用。')
    return tuple(values)


def reserve_model(provider,body):
    if CURRENT.get() is None:return None
    try:prices=model_prices(provider,body.get('model',''))
    except BudgetLimit as exc:
        CURRENT.get().stop(str(exc));raise
    # UTF-8 bytes exceed the text token count; add request-format overhead.
    messages=body.get('messages') or []
    text=json.dumps(messages,ensure_ascii=False)
    input_bound=len(text.encode())+1024
    if '"file_id"' in text:input_bound=max(input_bound,1048576)
    maximum=int(body.get('max_tokens') or body.get('max_completion_tokens') or 4096)
    return reserve((input_bound*prices[0]+maximum*prices[2])/1e6,provider+'_model')


def settle_model(ticket,provider,body,payload):
    if not ticket:return
    usage=payload.get('usage') if isinstance(payload,dict) else None
    if not isinstance(usage,dict) or 'prompt_tokens' not in usage or 'completion_tokens' not in usage:return
    p_in,p_hit,p_out=model_prices(provider,body.get('model',''))
    prompt=max(0,int(usage['prompt_tokens']));output=max(0,int(usage['completion_tokens']))
    cached=min(prompt,max(0,int(usage.get('prompt_cache_hit_tokens') or (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)))
    settle(ticket,((prompt-cached)*p_in+cached*p_hit+output*p_out)/1e6)


def page_count(data,filename):
    if filename.lower().endswith('.pdf'):
        try:
            import fitz
            with fitz.open(stream=data,filetype='pdf') as doc:return max(1,len(doc))
        except Exception:return 50  # Unknown document size uses an explicit conservative estimate.
    return 50 if filename.lower().endswith(('.doc','.docx')) else 1
