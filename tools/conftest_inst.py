# map-build only: inline pools + record which sqlglot classes each test instantiates
import concurrent.futures as cf, os, json, sys, atexit, collections
class InlineExecutor(cf.Executor):
    def __init__(self,*a,initializer=None,initargs=(),**k):
        if initializer: initializer(*initargs)
    def submit(self, fn, *a, **k):
        f = cf.Future()
        try: f.set_result(fn(*a, **k))
        except BaseException as e: f.set_exception(e)
        return f
    def shutdown(self,*a,**k): pass
cf.ProcessPoolExecutor = InlineExecutor
import sqlglot, pkgutil, importlib, inspect
for m in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):
    try: importlib.import_module(m.name)
    except Exception: pass
SEEN=collections.defaultdict(set)   # test nodeid -> set of type objects
_cur=[None]
def _wrap(cls):
    orig=cls.__dict__['__init__']
    def __init__(self,*a,**k):
        t=_cur[0]
        SEEN[t if t is not None else '<import>'].add(type(self))
        return orig(self,*a,**k)
    __init__.__wrapped__=orig
    cls.__init__=__init__
for name,mod in list(sys.modules.items()):
    if not name.startswith('sqlglot') or mod is None: continue
    for obj in list(vars(mod).values()):
        if inspect.isclass(obj) and obj.__module__==name:
            stack=[obj]
            while stack:
                c=stack.pop()
                if '__init__' in c.__dict__ and inspect.isfunction(c.__dict__['__init__']) and not hasattr(c.__dict__['__init__'],'__wrapped__'):
                    try: _wrap(c)
                    except Exception: pass
                stack+= [v for v in c.__dict__.values() if inspect.isclass(v) and v.__module__==name]
import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _cur[0]=item.nodeid; yield; _cur[0]=None
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _cur[0]=item.nodeid; yield; _cur[0]=None
def pytest_sessionfinish(session):
    out={}
    for t,types in SEEN.items():
        names=set()
        for ty in types:
            for b in ty.__mro__:
                if b.__module__.startswith('sqlglot'): names.add(f'{b.__module__}:{b.__qualname__}')
        out[t]=sorted(names)
    here = os.path.dirname(os.path.abspath(__file__))
    # tools/conftest_inst.py and the copy at sqlglot/conftest.py both sit one level below the repo.
    dest = os.path.join(os.path.dirname(here), 'maps', 'inst.json')
    json.dump(out, open(dest, 'w'))
