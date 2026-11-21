import json,os,re,subprocess,sys,shutil,tempfile,time
SQ='/home/claude/sq'; PY='/home/claude/v/bin/python'
T=json.load(open('/home/claude/kit/sample/tests.json'))['tests']
num={t['id']:t['number'] for t in T}; byfile={}
for t in T: byfile.setdefault(t['file'],[]).append(t['number'])
def truth(patch):
    w=tempfile.mkdtemp(dir='/tmp'); shutil.copytree(SQ,w+'/s',ignore=shutil.ignore_patterns('.git','conftest.py'),symlinks=True)
    r=subprocess.run(['patch','-p1','-s','-d',w+'/s','-i',patch],capture_output=True,text=True)
    if r.returncode: print('PATCHFAIL',r.stdout,r.stderr,file=sys.stderr)
    env=dict(os.environ,SKIP_INTEGRATION='0',PYTHONPATH='.:/home/claude/gt',GT_OUT=w+'/out.txt')
    t0=time.time()
    p=subprocess.run([PY,'-m','pytest','tests','-n','2','-q','-p','no:cacheprovider','-p','gtplug','--timeout=120'],cwd=w+'/s',env=env,capture_output=True,text=True)
    fail=set()
    for nid in open(w+'/out.txt').read().split('\n'):
        if not nid: continue
        if nid.startswith('COLLECT::'):
            f=nid[9:].split('::')[0]; fail|=set(byfile.get(f,[])); continue
        parts=nid.split('[')[0].split('::'); k='::'.join(parts[:3])
        if k in num: fail.add(num[k])
        else: print('UNMAPPED',nid[:120],file=sys.stderr)
    shutil.rmtree(w)
    print(os.path.basename(patch), f'{time.time()-t0:.0f}s', p.stdout.strip().splitlines()[-1][:120], file=sys.stderr)
    return sorted(fail)
if __name__=='__main__':
    out={}
    for p in sys.argv[1:]:
        out[os.path.basename(p)[:-6]]=truth(p)
        json.dump(out,open('/home/claude/gt/last.json','w'))
    print(json.dumps(out))
