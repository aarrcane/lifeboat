from lifeboat.models import Step


class Crash(BaseException):
    """Simulates the process dying (not caught by `except Exception`)."""


class FakeLease:
    def __init__(self):
        self.holder, self.transitions = None, 0


class FakeElector:
    def __init__(self, identity, lease: FakeLease):
        self.id, self.lease = identity, lease

    def try_acquire_or_renew(self):
        if self.lease.holder == self.id:
            return True, self.lease.transitions
        return False, self.lease.transitions

    def acquire_if_expired(self):            # test helper: lease of dead holder expired
        self.lease.holder, self.lease.transitions = self.id, self.lease.transitions + 1

    def still_leader(self, takeover):
        return self.lease.holder == self.id and self.lease.transitions == takeover


class FakeCluster:
    """Shared 'cluster' state: key -> current settings."""
    def __init__(self):
        self.state = {}
        self.apply_log = []


class FakeActions:
    def __init__(self, cluster: FakeCluster, crash_after_apply_of=None):
        self.c, self.crash_key = cluster, crash_after_apply_of

    def _target(self, s: Step):
        return {"suspend_job": {"suspend": True},
                "scale_deployment": {"replicas": s.params.get("replicas")},
                "set_configmap": {"data": s.params.get("data")}}[s.action]

    def read_original(self, s):
        return dict(self.c.state.get(s.key, {"suspend": False, "replicas": 3, "data": {}}))

    def apply(self, s):
        self.c.state[s.key] = self._target(s)
        self.c.apply_log.append(s.key)
        if self.crash_key == s.key:
            raise Crash()                     # dies after acting, before marking done

    def revert(self, s):
        self.c.state[s.key] = dict(s.original)

    def is_reverted(self, s):
        return self.c.state.get(s.key) == s.original

    def verify(self, s):
        return self.c.state.get(s.key) == self._target(s)
