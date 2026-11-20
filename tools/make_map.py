import json,sys,re,ast,collections,os
from coverage import CoverageData
T=json.load(open('sample/tests.json'))['tests']
idx={t['id']:t['number'] for t in T}
d=CoverageData('.coverage'); d.read()
# map context -> test number
ctxmap={}
for t in T:
    f,_,rest=t['id'].partition('::'); cls,_,fn=rest.partition('::')
    ctxmap[f[:-3].replace("/",".")+f".{cls}.{fn}"]=t["number"]
lines=collections.defaultdict(lambda: collections.defaultdict(set)); imp=collections.defaultdict(set)
filetests=collections.defaultdict(set); unk=set()
for f in d.measured_files():
    rel=os.path.relpath(f,'sqlglot')
    for ln,ctxs in d.contexts_by_lineno(f).items():
        for c in ctxs:
            if c=='': imp[rel].add(ln); continue
            n=ctxmap.get(c)
            if n is None: unk.add(c); continue
            lines[rel][ln].add(n); filetests[rel].add(n)
print('unmapped ctx',len(unk),list(unk)[:3],file=sys.stderr)
import pickle; pickle.dump(({k:dict(v) for k,v in lines.items()},dict(imp),dict(filetests)),open('maps/map.pkl','wb'))
