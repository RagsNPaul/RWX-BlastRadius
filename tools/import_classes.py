import sys, json
seen=set()
def prof(frame, event, arg):
    if event=='call' and frame.f_code.co_name=='__init__' and 'sqlglot' in frame.f_code.co_filename:
        s=frame.f_locals.get('self')
        if s is not None: seen.add(type(s))
sys.setprofile(prof)
import sqlglot, pkgutil, importlib
for m in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):
    try: importlib.import_module(m.name)
    except Exception: pass
sys.setprofile(None)
names=sorted({f'{b.__module__}:{b.__qualname__}' for t in seen for b in t.__mro__ if b.__module__.startswith('sqlglot')})
json.dump(names,open('/home/claude/inst/import_classes.json','w')); print(len(names), names[:15])
