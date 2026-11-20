import json,sys,re,ast,pickle,glob,os
SQ='sqlglot'; DATA=sys.argv[1]
T=json.load(open(f'{DATA}/tests.json'))['tests']; ms=[round(t['seconds']*1000) for t in T]
lines,imp,filetests=pickle.load(open('maps/map.pkl','rb'))
# fixture -> tests: test functions whose source mentions the fixture stem
fix={}
num={t['id']:t['number'] for t in T}
for tf in glob.glob(f'{SQ}/tests/**/*.py',recursive=True):
    src=open(tf).read(); rel=os.path.relpath(tf,SQ)
    for c in ast.walk(ast.parse(src)):
        if isinstance(c,ast.ClassDef):
            for fn in c.body:
                if isinstance(fn,ast.FunctionDef):
                    n=num.get(f'{rel}::{c.name}::{fn.name}')
                    if n is not None: fix[n]=ast.get_source_segment(src,fn)
def parse(p):
    out={};cur=None
    for l in p.splitlines():
        if l.startswith('+++ '): cur=l[6:]; out[cur]=[]
        elif l.startswith('@@'):
            m=re.match(r'@@ -(\d+)',l); o=int(m.group(1)); out[cur].append(['h']); 
        elif cur and l[:1] in ' -+':
            out[cur][-1].append((l[0],o,l[1:]))
            if l[0]!='+': o+=1
    return out
def apply(src,hunks):
    L=src.split('\n'); res=[];pos=1
    for h in hunks:
        rows=h[1:]; start=rows[0][1]
        res+=L[pos-1:start-1]; pos=start
        for k,o,t in rows:
            if k==' ': res.append(L[pos-1]); pos+=1
            elif k=='-': pos+=1
            else: res.append(t)
    res+=L[pos-1:]; return '\n'.join(res)
def pack(sel):
    if not sel: return []
    order=sorted(sel,key=lambda i:-ms[i]); best=None
    for k in range(1,9):
        W=[[] for _ in range(k)];L=[0]*k
        for i in order: j=L.index(min(L)); W[j].append(i); L[j]+=ms[i]
        c=max(L)+2000*k
        if best is None or c<best[0]: best=(c,W)
    return [w for w in best[1] if w]
for pf in sorted(glob.glob(f'{DATA}/commits/*.patch')):
    sel=set(); why=[]
    for f,hunks in parse(open(pf).read()).items():
        if f.endswith('.py') and f.startswith('sqlglot/'):
            old=open(f'{SQ}/{f}').read(); new=apply(old,hunks)
            if ast.dump(ast.parse(old))==ast.dump(ast.parse(new)): why.append('noop'); continue
            ch=set()
            for h in hunks:
                rows=h[1:]
                for i,(k,o,t) in enumerate(rows):
                    if k=='-': ch.add(o)
                    if k=='+' and not any(r[0]=='-' for r in rows): ch.update({o-1,o})
            for ln in ch:
                s=lines.get(f,{}).get(ln,set())
                if s: sel|=s; why.append('line')
                elif ln in imp.get(f,()): sel|=filetests.get(f,set()); why.append('import->file')
                else: why.append('uncovered')
        elif f.startswith('tests/fixtures/'):
            stem=f[len('tests/fixtures/'):-4]; base=stem.split('/')[-1]
            hit={n for n,s in fix.items() if s and (f'"{base}"' in s or stem in s or f"'{base}'" in s)}
            sel|=hit; why.append(f'fixture{len(hit)}')
        else: sel|=set(range(len(T))); why.append('ALL')
    W=pack(sel)
    print(json.dumps({"commit":os.path.basename(pf)[:-6],"workers":W}))
    print(os.path.basename(pf)[:-6],len(sel),round(sum(ms[i] for i in sel)/1000,1),why,file=sys.stderr)
