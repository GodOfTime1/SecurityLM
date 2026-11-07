"""Advisor-specific configuration and typed adapter boundary. No connection at startup."""
import math
import os
import re
from dataclasses import dataclass, replace
from typing import Protocol
from .model_client import EchoModel, ModelRequest, ModelResponse, ModelUnavailable, OllamaModel


class AdvisorAdapter(Protocol):
    """A future Empire/vLLM adapter implements this one method."""
    def generate(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class AdvisorSettings:
    backend: str
    model: str = ''
    base_url: str = ''
    timeout: float = 60
    allow_remote: bool = False


def get_advisor_settings() -> AdvisorSettings:
    backend = os.environ.get('ADVISOR_BACKEND', 'echo')
    if backend not in {'disabled', 'echo', 'ollama'}:
        raise ModelUnavailable('Advisor backend is not supported; configure disabled, echo, or ollama')
    if backend != 'ollama':
        return AdvisorSettings(backend=backend)
    model = os.environ.get('ADVISOR_MODEL', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}', model):
        raise ModelUnavailable('Set ADVISOR_MODEL to the advisor runtime model tag')
    try:
        timeout = float(os.environ.get('ADVISOR_TIMEOUT_SECONDS', '60'))
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ValueError
    except ValueError:
        raise ModelUnavailable('ADVISOR_TIMEOUT_SECONDS must be from 1 through 120') from None
    remote = os.environ.get('ADVISOR_ALLOW_REMOTE', '0')
    if remote not in {'0', '1'}:
        raise ModelUnavailable('ADVISOR_ALLOW_REMOTE must be 0 or 1')
    config = AdvisorSettings(backend, model, os.environ.get('ADVISOR_BASE_URL','http://127.0.0.1:11434'), timeout, remote=='1')
    # Reuse URL policy validation without contacting a server. The explicit flag
    # prevents document-chat OLLAMA_ALLOW_REMOTE from authorizing advisor traffic.
    OllamaModel(config.base_url, config.model, config.timeout, allow_remote=config.allow_remote)
    return config


@dataclass
class AdvisorClient:
    adapter: AdvisorAdapter
    settings: AdvisorSettings

    def generate(self, request: ModelRequest) -> ModelResponse:
        if request.mode != 'model_selector' or request.tools:
            raise ModelUnavailable('Advisor accepts selector requests without tools only')
        # The deployment's advisor choice wins over any caller-supplied model.
        return self.adapter.generate(replace(request, model=self.settings.model))


def get_advisor_client() -> AdvisorClient:
    config = get_advisor_settings()
    if config.backend == 'disabled':
        raise ModelUnavailable('Advisor is disabled until a runtime is configured')
    adapter = (EchoModel() if config.backend == 'echo' else
               OllamaModel(config.base_url,config.model,config.timeout,allow_remote=config.allow_remote))
    return AdvisorClient(adapter, config)
