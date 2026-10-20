"""Frozen, constructed coding-transfer pilot, not SWE-bench or a public benchmark.

Historical facts are supplied equally to both memory variants. The none condition
is a cold-session control. Tests are kept outside the agent workspace.
"""

CASES = [
    {
        "id": "leased_workspace", "category": "transfer",
        "history": [
            "Earlier SDK integration: work items use lease_store under the workspace module. "
            "The public edit API is edit(store, key, transform). store.lease(key) is a context manager. "
            "Acquire it BEFORE store.load(key), then call transform(old), store.save(key,new), return new. "
            "Release on success and on any exception. Locking only save loses updates. "
            "The transport SDK intentionally does not expose a global process lock."
        ],
        "task": "Implement workspaces.edit using the existing store interface and our established ownership protocol. "
                "The transform may fail, and two workers may update the same key. Preserve its return value.",
        "files": {"workspaces.py": "def edit(store, key, transform):\n    raise NotImplementedError\n"},
        "check": """
from workspaces import edit
from contextlib import contextmanager
class Store:
    def __init__(self): self.held=False; self.data=10; self.events=[]
    @contextmanager
    def lease(self,key):
        assert not self.held
        self.held=True; self.events.append('lock')
        try: yield
        finally: self.held=False; self.events.append('unlock')
    def load(self,key):
        assert self.held, 'load without lease'
        self.events.append('load'); return self.data
    def save(self,key,value):
        assert self.held; self.events.append('save'); self.data=value
s=Store()
assert edit(s,'a',lambda x:x+2)==12
assert s.events==['lock','load','save','unlock']
try: edit(s,'a',lambda x:1/0)
except ZeroDivisionError: pass
else: raise AssertionError('exception swallowed')
assert not s.held and s.data==12
""",
    },
    {
        "id": "clock_units", "category": "transfer",
        "history": [
            "Incident 84 in Orbit cache: do not confuse vendor's millisecond timestamps with retention seconds. "
            "Record field received is Unix milliseconds; window_s is seconds; now_ms is milliseconds. "
            "Keep records whose age is strictly less than window_s*1000; equality is expired. "
            "Future timestamps must remain, input order must be preserved, and never mutate input dictionaries. "
            "A later reader consumes the original received field unchanged."
        ],
        "task": "Implement cache_window.live(records, now_ms, window_s) for our Orbit records. "
                "Apply the existing time-unit and boundary conventions. Return surviving records in input order.",
        "files": {"cache_window.py": "def live(records, now_ms, window_s):\n    return records\n"},
        "check": """
from cache_window import live
rows=[{'received':7000,'id':'edge'},{'received':7001,'id':'in'},{'received':10001,'id':'future'},{'received':1000,'id':'old'}]
snapshot=[dict(r) for r in rows]
assert [r['id'] for r in live(rows,10000,3)]==['in','future']
assert rows==snapshot
assert live([],0,4)==[]
assert [r['id'] for r in live(rows,10000,0)]==['future']
""",
    },
    {
        "id": "approval_wakeup", "category": "transfer",
        "history": [
            "Review gate race diagnosis: reset the shared result and clear the Event before publishing a new "
            "approval request. Publishing can synchronously deliver an answer; clearing after publish loses "
            "that wakeup. Main UI writes result['decision'] before event.set(). "
            "request(event,result,publish) must wait for the event, then return result['decision']. "
            "Never use busy waiting. Historical stale decisions must not survive into publish."
        ],
        "task": "Repair review_gate.request. A frontend may answer synchronously inside publish(), "
                "and stale approval from a previous request must not leak.",
        "files": {"review_gate.py": "def request(event, result, publish):\n    publish()\n    event.clear()\n    event.wait()\n    return result['decision']\n"},
        "check": """
from review_gate import request
class Event:
    def __init__(self): self.flag=True; self.actions=[]
    def clear(self): self.flag=False; self.actions.append('clear')
    def set(self): self.flag=True; self.actions.append('set')
    def wait(self):
        self.actions.append('wait'); assert self.flag, 'lost wakeup would block'
event=Event(); result={'decision':'old'}
def publish():
    assert not event.flag and 'decision' not in result
    result['decision']='deny'; event.set()
assert request(event,result,publish)=='deny'
assert event.actions==['clear','set','wait']
""",
    },
    {
        "id": "paired_boundary", "category": "transfer",
        "history": [
            "Conversation reducer design: messages before cut are summarized, messages from cut onward remain. "
            "Internal call messages have role='assistant_tool_call' and toolUseId; results have role='tool_result' "
            "with the same toolUseId. Legal cut roles are user, assistant, assistant_progress, assistant_tool_call. "
            "A cut is illegal when any removed tool call ID appears among retained tool results. "
            "Pick the first legal index AT OR AFTER the proposed index, never move toward older messages. "
            "If none exists return len(messages), meaning summarize the complete span. "
            "Do not treat a result as a legal start, even when its pairing is malformed."
        ],
        "task": "Implement boundaries.choose(messages, proposed) for our conversation reducer, preserving "
                "the established internal role format and the call/result invariant.",
        "files": {"boundaries.py": "def choose(messages, proposed):\n    return proposed\n"},
        "check": """
from boundaries import choose
def c(i): return {'role':'assistant_tool_call','toolUseId':i}
def r(i): return {'role':'tool_result','toolUseId':i}
u={'role':'user','content':'go'}; a={'role':'assistant','content':'done'}
assert choose([u,c('A'),c('B'),r('B'),r('A'),a],2)==5
assert choose([u,c('A'),c('B'),r('B'),r('A')],2)==5
assert choose([u,c('A'),r('A'),a],1)==1
assert choose([u,c('A'),r('A'),a],2)==3
assert choose([u,a],0)==0
assert choose([],0)==0
""",
    },
    {
        "id": "stale_retry_policy", "category": "knowledge_update",
        "history": [
            "Old Vela request contract: transient statuses 408, 429 and all 5xx should retry. "
            "For the original client, 409 conflicts also retried.",
            "Correction after the Vela v2 deployment: 409 is a permanent business conflict and must NEVER retry. "
            "Retry only 408, 429, or integers from 500 through 599. All other statuses, including 600+, "
            "are permanent. This supersedes the earlier retry-all-conflicts policy for Vela. "
            "The public function retryable(status) returns a boolean."
        ],
        "task": "Implement vela.retryable(status) for our current client. Use the latest agreed transport policy, "
                "including the conflict handling change.",
        "files": {"vela.py": "def retryable(status):\n    return status >= 400\n"},
        "check": """
from vela import retryable
for x in [408,429,500,502,599]: assert retryable(x) is True, x
for x in [200,301,400,401,403,404,409,499,600,999]: assert retryable(x) is False, x
""",
    },
    {
        "id": "new_unrelated_task", "category": "negative_control",
        "history": [
            "An unrelated presentation project uses uppercase deduplication for slide labels. "
            "Its normalize_labels converts every label to upper case and sorts alphabetically. "
            "That convention only applies to slide labels, not arbitrary APIs."
        ],
        "task": "Implement labels.unique(values): keep the FIRST occurrence of each exact string, "
                "preserve order and case, and return a new list. This API is unrelated to the slide-label project.",
        "files": {"labels.py": "def unique(values):\n    return sorted(set(v.upper() for v in values))\n"},
        "check": """
from labels import unique
v=['b','A','a','b','A','c']; before=list(v)
assert unique(v)==['b','A','a','c']
assert v==before
assert unique([])==[]
assert unique(['X','x'])==['X','x']
""",
    },
]

# Noise is independent of task labels. Included in both searchable histories.
DISTRACTORS = [
    "Build dashboard uses midnight blue and pale green; these are presentation-only colors.",
    "A separate archive utility maps document IDs to stable UUIDs and exports zip manifests.",
    "The image thumbnail job must preserve aspect ratio and leave original photographs untouched.",
    "For a different mobile repository, run unit tests with a simulator and code signing disabled.",
    "Billing reports group amounts by currency and never add different currencies directly.",
    "A prior frontend task removed an unused CSS class after screenshot verification.",
]
