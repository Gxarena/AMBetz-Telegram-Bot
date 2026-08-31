"""Process-wide Secret Manager cache.

Cloud Run instances were paying Secret Manager on every GCPTelegramBot /
GCPStripeService construct (dozens of AccessSecretVersion calls). Cache each
secret name for the life of the process, including failed lookups so missing
optional secrets are not retried on every request.
"""

import logging
from typing import Dict, Optional

from google.cloud import secretmanager

logger = logging.getLogger(__name__)

_cache: Dict[str, str] = {}
_client: Optional[secretmanager.SecretManagerServiceClient] = None


def _sm_client() -> secretmanager.SecretManagerServiceClient:
    global _client
    if _client is None:
        _client = secretmanager.SecretManagerServiceClient()
    return _client


def access_secret(project_id: str, secret_name: str) -> str:
    if not project_id or not secret_name:
        return ""
    cache_key = f"{project_id}/{secret_name}"
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = _sm_client().access_secret_version(request={"name": name})
        val = response.payload.data.decode("UTF-8")
        _cache[cache_key] = val
        return val
    except Exception as e:
        logger.error("Error accessing secret %s: %s", secret_name, e)
        _cache[cache_key] = ""
        return ""
