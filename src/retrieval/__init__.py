from src.retrieval.fetcher import FetchError, FetchResult, fetch, fetch_many
from src.retrieval.session import close_client, get_client

__all__ = ["fetch", "fetch_many", "FetchResult", "FetchError", "get_client", "close_client"]
