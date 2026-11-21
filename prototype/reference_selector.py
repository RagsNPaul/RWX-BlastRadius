import json,sys,re,ast,gzip,glob,os
SQ='/home/claude/sq'; DATA=sys.argv[1]
T=json.load(open(f'{DATA}/tests.json'))['tests']; ALL=set(range(len(T)))
M='/mnt/user-data/uploads/RWZ-BlastRadius/maps'
S=json.load(open(f'{M}/sets.json')); I=json.load(open(f'{M}/index.json'))
class _L(dict):
    def get(self,f,d=None):
        p=f'{M}/lines/{f}.json'
        if not os.path.exists(p): return d
        return {k:S[v] for k,v in json.load(open(p)).items()}
LINES=_L(); IMP={f:set(v) for f,v in I['import_lines'].items()}; FT=I['file_union']; OPENS=I['opens']
src=open('/home/claude/sel.py').read(); ns={}; exec('import re\n'+src[src.index('def parse'):src.index('def pack')],ns); parse,apply=ns['parse'],ns['apply']
num={t['id']:t['number'] for t in T}; bodies={}
INST={}; IMPCLS=set(json.load(open('/home/claude/inst/import_classes.json'))); VARIANT=os.environ.get('V','A')
for nid,names in json.load(open('/home/claude/inst/inst.json')).items():
    k='::'.join(nid.split('[')[0].split('::')[:3])
    if k in num:
        for c in names: INST.setdefault(c,set()).add(num[k])
def class_chain(tree,ln):
    # innermost class-body (not inside a function) containing ln -> qualname
    best=None
    def walk(node,prefix):
        nonlocal best
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch,ast.ClassDef) and ch.lineno<=ln<=ch.end_lineno:
                q=prefix+[ch.name]; best=q; walk(ch,q)
            elif isinstance(ch,(ast.FunctionDef,ast.AsyncFunctionDef)) and ch.lineno<=ln<=ch.end_lineno:
                if best and len(best)==len(prefix): best=None
                return
    walk(tree,[])
    return '.'.join(best) if best else None
for tf in glob.glob(f'{SQ}/tests/**/*.py',recursive=True):
    s=open(tf).read(); rel=os.path.relpath(tf,SQ)
    for c in ast.walk(ast.parse(s)):
        if isinstance(c,ast.ClassDef):
            for fn in c.body:
                if isinstance(fn,ast.FunctionDef):
                    n=num.get(f'{rel}::{c.name}::{fn.name}')
                    if n is not None: bodies[n]=ast.get_source_segment(s,fn)
def span(tree,ln):
    best=None
    for n in ast.walk(tree):
        if isinstance(n,ast.stmt) and n.lineno<=ln<=n.end_lineno:
            b=getattr(n,'body',None); end=n.end_lineno
            if isinstance(b,list) and b: end=b[0].lineno-1
            if ln>end: continue
            if best is None or end-n.lineno<best[1]-best[0]: best=(n.lineno,end)
    return range(best[0],best[1]+1) if best else [ln]
for pf in sorted(glob.glob(f'{DATA}/commits/*.patch')):
    sel=set(); why=[]
    for f,h in parse(open(pf).read()).items():
        if f.startswith('sqlglot/') and f.endswith('.py') and os.path.exists(f'{SQ}/{f}'):
            old=open(f'{SQ}/{f}').read(); new=apply(old,h)
            try: tr=ast.parse(old); nt=ast.parse(new)
            except SyntaxError: sel|=ALL; why.append('syntax'); continue
            if ast.dump(tr)==ast.dump(nt): why.append('noop'); continue
            L=LINES.get(f,{}); ch=set()
            for hh in h:
                rows=hh[1:]
                for k,o,t in rows:
                    if k=='-': ch.add(o)
                if not any(r[0]=='-' for r in rows): ch|={o for k,o,t in rows if k=='+'}|{o-1 for k,o,t in rows if k=='+'}
            for ln in ch:
                sp=span(tr,ln); s=set().union(*[set(L.get(str(x),[])) for x in sp])
                if s:
                    sel|=s; why.append('line')
                    if VARIANT=='B' and any(x in IMP.get(f,()) for x in sp) and class_chain(tr,ln) is None:
                        sel|=ALL; why.append('IMPORTLINE->ALL')
                elif any(x in IMP.get(f,()) for x in sp):
                    sel|=set(FT.get(f,ALL)); why.append('import')
                    q=class_chain(tr,ln)
                    if q:
                        mod=f[:-3].replace('/','.')
                        if mod.endswith('.__init__'): mod=mod[:-9]
                        hit=INST.get(f'{mod}:{q}',set()); sel|=hit; why.append(f'cls:{q}:{len(hit)}')
                        if f'{mod}:{q}' in IMPCLS: sel|=ALL; why.append('IMPORTCLASS->ALL')
                else: why.append('uncov')
        elif f.startswith('tests/fixtures/'):
            stem=f[15:].rsplit('.',1)[0]; base=stem.split('/')[-1]
            hit={n for n,s in bodies.items() if s and (f'"{base}' in s or f"'{base}" in s or stem in s)}
            sel|=set(OPENS.get(f,[])) or hit or ALL; why.append('fix')
        elif f.startswith('tests/') and f.endswith('.py') and 'helpers' not in f and 'conftest' not in f:
            sel|={t['number'] for t in T if t['file']==f}; why.append('testfile')
        else: sel|=ALL; why.append('ALL')
    print(json.dumps({"commit":os.path.basename(pf)[:-6],"workers":[sorted(sel)] if sel else []}))
    print(os.path.basename(pf)[:-6],len(sel),why,file=sys.stderr)
