"""Creator-bound execution and bounded in-memory user credential availability.

Forwarded Apps tokens have no refresh grant. This store is NOT a durable token
vault: restart, explicit revocation or a ten-minute lease expires authorization.
A company-approved durable resolver can be injected server-side later. No HTTP
request may choose a resolver, principal, workspace or credential.
"""
from dataclasses import dataclass
import threading
import time

from fastapi import HTTPException
from hub_auth import current_identity, owns, verified_identity

PERSONAL_MODES=frozenset(('apps_obo','oauth_user','obo_user'))


def require_creator(task,identity=None):
    user=identity or current_identity()
    security=task.get('report',{}).get('security',{})
    if not owns(task,user) or security.get('execution_authority')!='creator':
        raise PermissionError('Task is not bound to this creator; no shared identity fallback')
    return user


def verify_creator(task,identity=None):
    user=require_creator(task,identity)
    actual=verified_identity(user.client,'obo_user')
    if actual.key!=user.key:
        raise PermissionError('Actual credential does not belong to task creator')
    return user


@dataclass(repr=False)
class Lease:
    identity: object
    expires: float


class CreatorAuthorizations:
    def __init__(self,*,ttl=600,clock=time.monotonic,capacity=500):
        if not 0<ttl<=600 or capacity<1:raise ValueError('Invalid authorization bounds')
        self.ttl,self.clock,self.capacity=ttl,clock,capacity
        self.lock=threading.RLock()
        self.leases={}

    def _prune(self):
        now=self.clock()
        self.leases={key:value for key,value in self.leases.items() if value.expires>now}

    def remember(self,identity):
        if identity.mode not in PERSONAL_MODES:
            return
        with self.lock:
            self._prune()
            if identity.key not in self.leases and len(self.leases)>=self.capacity:
                raise HTTPException(429,'授权容量已满，请稍后重试。')
            self.leases[identity.key]=Lease(identity,self.clock()+self.ttl)

    def revoke(self,identity):
        with self.lock:self.leases.pop(identity.key,None)

    def status(self,identity):
        with self.lock:
            self._prune();lease=self.leases.get(identity.key)
            remaining=max(0,int(lease.expires-self.clock())) if lease else 0
        return {'authorized':lease is not None,'remaining_seconds':remaining,
            'authority':'creator','storage':'server_memory','durable':False,
            'scheduler_started':False,
            'note':'任务自动绑定创建者。这里只表示内存中用户凭据的可用性；不代表长期后台授权已就绪。'}

    def resolve(self,task):
        security=task.get('report',{}).get('security',{})
        workspace,subject=security.get('workspace'),security.get('creator_subject')
        if not workspace or not subject or security.get('execution_authority')!='creator':
            raise PermissionError('Trusted task creator binding required')
        key=workspace+':'+subject
        with self.lock:
            self._prune();lease=self.leases.get(key)
            if lease is None:raise PermissionError('Creator authorization missing or expired')
        try:user=verify_creator(task,lease.identity)
        except Exception:
            with self.lock:
                if self.leases.get(key) is lease:self.leases.pop(key,None)
            raise PermissionError('Creator credential invalid or revoked') from None
        # Reject a lease revoked/replaced while identity verification was in flight.
        with self.lock:
            if self.leases.get(key) is not lease or lease.expires<=self.clock():
                raise PermissionError('Creator authorization expired or revoked')
        return user


def resolve_for_task(manager,task):
    """One resolver shared by dispatch and workers; future broker is server-only."""
    resolver=getattr(manager,'creator_resolver',None)
    if resolver is None:return manager.task_authorizations.resolve(task)
    identity=resolver(task)
    if identity is None:raise PermissionError('Creator credential unavailable')
    return verify_creator(task,identity)
