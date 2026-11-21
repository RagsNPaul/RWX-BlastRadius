import json,sys
T=json.load(open('/home/claude/kit/sample/tests.json'))['tests'];A=json.load(open('/home/claude/kit/sample/answers.json'))['commits']
B=json.load(open('/home/claude/gt/last.json')); ms=[round(t['seconds']*1000) for t in T]
def best(sel):
    if not sel: return 0
    d=sorted((ms[i] for i in sel),reverse=True); b=1e18
    for k in range(1,9):
        L=[0]*k
        for x in d: L[L.index(min(L))]+=x
        b=min(b,max(L)+2000*k)
    return b/1000
def ev(p,truth,name):
    tot=0;miss=[]
    for l in open(p):
        o=json.loads(l); s={i for w in o['workers'] for i in w}; c=o['commit']
        m=[i for i in truth[c] if i not in s]; tot+=best(s)
        if m: miss.append(c)
    print(f'  {name}: simulated {tot:.1f}s ({tot/len(truth):.2f}/commit) misses {len(miss)} {miss}')
ev(sys.argv[1],{k:v['failing'] for k,v in A.items()},'samples (30)')
ev(sys.argv[2],B,'test patches (24)')
