"""Former home of the column-key helpers, kept so existing imports work.

They live in sheetrender.column_keys now; this name was easy to confuse with
html_sanitize, which cleans HTML.
"""
from sheetrender.column_keys import sanitize_column, sanitize_columns

__all__ = ["sanitize_column", "sanitize_columns"]
