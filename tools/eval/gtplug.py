import os
_out=set()
def pytest_runtest_logreport(report):
    if report.failed: _out.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed: _out.add('COLLECT::'+report.nodeid)
def pytest_sessionfinish(session):
    if not hasattr(session.config,'workerinput'):
        open(os.environ['GT_OUT'],'w').write('\n'.join(sorted(_out)))
