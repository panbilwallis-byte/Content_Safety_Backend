"""Do not retain bearer download capabilities in the HTTP access log."""
import logging


class DownloadTicketFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn access arguments: client, method, full_path, HTTP version, status.
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            path = record.args[2]
            if isinstance(path, str) and path.partition("?")[0] == "/api/v1/compliance/agent-traces/downloads":
                args = list(record.args)
                args[2] = path.partition("?")[0] + "?t=[REDACTED]"
                record.args = tuple(args)
        return True


def protect_download_access_logs():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, DownloadTicketFilter) for f in logger.filters):
        logger.addFilter(DownloadTicketFilter())
