"""Context shared by a transfer and its progress ticker."""
from contextvars import ContextVar

transfer_id = ContextVar('transfer_id', default=None)
