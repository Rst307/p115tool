"""Ephemeral, credential-free account observations with explicit freshness."""
from copy import deepcopy
import threading
import time


class AccountMonitor:
    def __init__(self, service):
        self.service=service
        self._lock=threading.RLock()
        self._attempted=None
        self._data={'state':'UNCHECKED','logged_in':None,'checked_at':None,'capacity':None,'capacity_state':'UNCHECKED'}

    def snapshot(self, refresh=False, force=False):
        with self._lock:
            age=None if self._attempted is None else max(0,time.monotonic()-self._attempted)
            stale=age is None or age>=self.service.config.account_status_ttl
            if refresh and (force or stale):
                self.service.available()
                data={'state':'UNKNOWN','logged_in':None,'checked_at':time.time(),'capacity':None,'capacity_state':'UNAVAILABLE'}
                try:
                    logged_in=self.service.client.login_status()
                    if type(logged_in) is not bool:
                        raise ValueError('Unsupported login result')
                    data.update(state='AUTHENTICATED' if logged_in else 'EXPIRED',logged_in=logged_in)
                    if logged_in:
                        try:
                            capacity=self.service.client.account_capacity()
                            if capacity is not None:
                                data.update(capacity=capacity,capacity_state='AVAILABLE')
                        except Exception:
                            pass  # Capacity failure does not falsify login observation.
                except Exception:
                    pass  # Never expose upstream exception details or raw account data.
                self._data=data
                self._attempted=time.monotonic()
                age,stale=0,False
            result=deepcopy(self._data)
            result.update(age_seconds=age,stale=stale,ttl_seconds=self.service.config.account_status_ttl)
            return result
