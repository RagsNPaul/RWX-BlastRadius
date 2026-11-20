# map-build only: run pool work inline so coverage attributes it to the calling test
import concurrent.futures as cf
class InlineExecutor(cf.Executor):
    def __init__(self,*a,**k): pass
    def submit(self, fn, *a, **k):
        f = cf.Future()
        try: f.set_result(fn(*a, **k))
        except BaseException as e: f.set_exception(e)
        return f
    def shutdown(self,*a,**k): pass
cf.ProcessPoolExecutor = InlineExecutor
