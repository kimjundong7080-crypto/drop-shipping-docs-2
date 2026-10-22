import logging
import re

_MASK_KEYS = re.compile(
    r"(access[_-]?token|authorization|api[_-]?key|client[_-]?secret|"
    r"open[_-]?id|sign|password|cookie|shpat_[A-Za-z0-9]+)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)")
_PHONE = re.compile(r"\b(\+?\d{1,3})?[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{4}\b")


class PIIRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        msg = _MASK_KEYS.sub(r"\1=***", msg)
        msg = _EMAIL.sub(r"\1***@\2", msg)
        msg = _PHONE.sub("***-****-****", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s :: %(message)s"
    ))
    handler.addFilter(PIIRedactionFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
