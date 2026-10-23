"""Single-process periodic driver. Construction never starts a timer or SDK call."""
from datetime import datetime, timezone
import threading
import time


class SchedulerLoop:
    def __init__(self, service, execution_scope, resolve_identity, *, interval_seconds=60, clock=None):
        if not callable(execution_scope) or not callable(resolve_identity):
            raise ValueError('Explicit execution scope and creator identity resolver required')
        if not 0 < interval_seconds <= 3600:
            raise ValueError('Invalid scheduler check interval')
        self.service=service
        self.execution_scope=execution_scope
        self.resolve_identity=resolve_identity
        self.interval_seconds=interval_seconds
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self._stop=threading.Event()
        self._tick_lock=threading.Lock()
        self._state_lock=threading.RLock()
        self._thread=None
        self._state='disabled'
        self._ticks=0
        self._last=None

    def status(self):
        with self._state_lock:
            return {'enabled':self._state in ('running','stopping'), 'state':self._state,
                    'interval_seconds':self.interval_seconds,'ticks':self._ticks,
                    'last_tick':dict(self._last) if self._last else None,
                    'automatic_email':False,'single_process_only':True}

    def tick(self, *, enabled=False):
        if not enabled:raise RuntimeError('Scheduler disabled; explicit opt-in required')
        if not self._tick_lock.acquire(blocking=False):return {'state':'busy','runs':[]}
        started=time.monotonic()
        last={'checked_at':None,'state':'failed','runs':[]}
        try:
            now=self.clock()
            last['checked_at']=now.isoformat()
            # No browser ContextVar is implicitly copied into the background thread.
            # The trusted caller must provide the authorized task-enumeration scope.
            with self.execution_scope():
                result=self.service.dispatch_once(now,self.resolve_identity,enabled=True)
            last.update(state='completed',runs=result['runs'])
            return result
        except Exception as exc:
            last.update(state='failed',error_type=type(exc).__name__)
            raise
        finally:
            last['duration_ms']=round((time.monotonic()-started)*1000,3)
            with self._state_lock:
                self._ticks+=1
                self._last=last
            self._tick_lock.release()

    def start(self, *, enabled=False):
        if not enabled:raise RuntimeError('Scheduler disabled; explicit opt-in required')
        with self._state_lock:
            if self._thread and self._thread.is_alive():return False
            self._stop.clear()
            self._state='running'
            self._thread=threading.Thread(target=self._run,name='hub-scheduler',daemon=True)
            try:self._thread.start()
            except Exception:
                self._state='stopped'
                raise
            return True

    def _run(self):
        try:
            while not self._stop.is_set():
                started=time.monotonic()
                try:self.tick(enabled=True)
                except Exception:
                    # Retain the error type in status, never log credentials/raw SDK errors.
                    # Existing run claims prevent another report for the same slot.
                    pass
                self._stop.wait(max(0.01,self.interval_seconds-(time.monotonic()-started)))
        finally:
            with self._state_lock:self._state='stopped'

    def stop(self, timeout=5):
        with self._state_lock:
            self._stop.set()
            thread=self._thread
            if thread and thread.is_alive():self._state='stopping'
            else:self._state='stopped'
        if thread and thread is not threading.current_thread():thread.join(timeout)
        # Already claimed work is not cancelled and no email is dispatched here.
        return not thread or not thread.is_alive()
