"""Workspace files remain private before account data or job history is accessed."""
import os
import pytest

from backend.api import Jobs


def test_workspace_is_private_under_permissive_umask_and_hardens_existing_file(tmp_path):
    directory=tmp_path/'workspace';directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    path=directory/'workspace.db'
    previous=os.umask(0)
    try:
        jobs=Jobs(path)
    finally:
        os.umask(previous)
    assert path.stat().st_mode & 0o777==0o600
    jobs.save_connection({'source':{'example':'preserve'},'thresholds':{}})
    path.chmod(0o644)
    reopened=Jobs(path)
    assert path.stat().st_mode & 0o777==0o600
    assert reopened.connection()['source']=={'example':'preserve'}
    assert directory.stat().st_mode & 0o777==0o755
    # The account CLI opens existing storage without application initialization.
    path.chmod(0o644)
    local_operator=object.__new__(Jobs);local_operator.path=str(path)
    assert local_operator.connection()['source']=={'example':'preserve'}
    assert path.stat().st_mode & 0o777==0o600


def test_workspace_permission_failure_refuses_sqlite_access_and_closes_descriptor(tmp_path,monkeypatch):
    path=tmp_path/'workspace.db';Jobs(path)
    path.chmod(0o644);original=path.read_bytes();descriptors=[]
    def denied(descriptor,mode):
        descriptors.append(descriptor)
        raise PermissionError('owner-only permissions could not be enforced')
    monkeypatch.setattr('backend.api.os.fchmod',denied)
    monkeypatch.setattr('backend.api.sqlite3.connect',lambda *args,**kwargs:pytest.fail('SQLite must not open after permission hardening fails'))
    with pytest.raises(PermissionError):
        Jobs(path)
    assert path.read_bytes()==original
    assert descriptors
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
