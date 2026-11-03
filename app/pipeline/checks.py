from dataclasses import asdict, dataclass

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str      # PASS | WARN (needs approval) | FAIL (exception)
    message: str

    def dict(self):
        return asdict(self)
