"""Shared governor test fakes (tests/ is not a package: import as `governor_fakes`)."""


class FakeSys:
    def __init__(self, mem=16.0, tmp=10.0, disk=50.0, load=4.0):
        self.mem, self.tmp, self.disk, self.load = mem, tmp, disk, load
    def mem_available_gb(self): return self.mem
    def tmp_used_pct(self): return self.tmp
    def disk_used_pct(self): return self.disk
    def load5(self): return self.load
