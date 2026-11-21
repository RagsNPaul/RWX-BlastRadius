import gzip,json,random,re,ast,difflib,os,sys
SQ='/home/claude/sq'; random.seed(int(sys.argv[1])); N=int(sys.argv[2]); OUT=sys.argv[3]
R=json.load(gzip.open('/home/claude/rchk/maps/map.json.gz','rt'))
OPS=[(r'==',r'!='),(r'!=',r'=='),(r' and ',r' or '),(r' or ',r' and '),(r' is not ',r' is '),(r' is (?!not)',r' is not '),
     (r'<=',r'<'),(r'(?<![<=])<(?![<=])',r'<='),(r'>=',r'>'),(r'(?<![>=-])>(?![>=])',r'>='),(r'\bTrue\b','False'),(r'\bFalse\b','True'),
     (r'\bnot ',''),(r'\+ 1\b','- 1'),(r'- 1\b','+ 1'),(r'\b0\b','1'),(r'\b1\b','2'),(r'"([A-Z_]{2,})"',r'"\1X"')]
cands=[]
for f,L in R['lines'].items():
    if not f.startswith('sqlglot/'): continue
    for ln in L: cands.append((f,int(ln)))
for f,ls in R['import_lines'].items():
    for ln in ls: cands.append((f,int(ln)))   # include class-level/import-time lines
random.shuffle(cands); made=0; os.makedirs(OUT,exist_ok=True)
for f,ln in cands:
    if made>=N: break
    src=open(f'{SQ}/{f}').read().split('\n'); line=src[ln-1]
    if line.strip().startswith(('#','"""','def ','class ','import','from ','@')): continue
    ops=[o for o in OPS if re.search(o[0],line.split('#')[0])]
    if not ops: continue
    a,b=random.choice(ops); new=re.sub(a,b,line,count=1)
    if new==line: continue
    s2=src[:]; s2[ln-1]=new
    try:
        if ast.dump(ast.parse('\n'.join(src)))==ast.dump(ast.parse('\n'.join(s2))): continue
    except SyntaxError: continue
    d=''.join(difflib.unified_diff([x+'\n' for x in src],[x+'\n' for x in s2],f'a/{f}',f'b/{f}',n=3))
    mid=f'r{sys.argv[1]}_{made:02d}'; open(f'{OUT}/{mid}.patch','w').write(f'diff --git a/{f} b/{f}\n'+d); made+=1
    print(mid,f,ln,line.strip()[:60],'=>',new.strip()[:60])
