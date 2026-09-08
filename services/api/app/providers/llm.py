"""Language model access for the operator assistant.

Three constraints shape this module, and all three come from what the assistant
is for. It is a natural-language interface over validated backend tools — not the
routing engine, not the risk engine, and not a source of truth. It runs on a
model hosted on the operator's own hardware, because operational logistics data
for a border region should not be shipped to a third party to be summarised. And
the core product must work with it switched off: risk scoring, routing and alerts
do not call anything here.

So a provider in this module returns two things and decides nothing: text, and a
list of tool calls the model would like performed. :class:`LLMToolCall` is a
request. The Phase 12 dispatcher checks the name against an allowlist, validates
the arguments against a schema, applies the caller's own permissions, and only
then runs anything. A model asking for a tool the user may not use is an ordinary
event, and the answer is no.

When the copilot is disabled — the default — the registry hands back
:class:`DisabledLLMProvider`, whose every method raises, so the assistant
endpoints return 503 instead of quietly degrading into something that looks like
an answer.
"""

from __future__ import annotations

import abc
import json
from typing import Any, ClassVar, Dict, List, Optional, Sequence

from app.core.enums import DataProvenance
from app.providers.base import (
    Provider,
    ProviderResponseError,
    ProviderStatus,
    ProviderUnavailable,
    UnconfiguredProvider,
)
from app.providers.models import LLMMessage, LLMResponse, LLMToolCall

__all__ = [
    "DisabledLLMProvider",
    "LLMProvider",
    "OllamaLLMProvider",
    "OpenAICompatibleLLMProvider",
    "StubLLMProvider",
]


class LLMProvider(Provider):
    """Chat completion, with optional tool-calling."""

    kind: ClassVar[str] = "llm"
    #: Model output is derived from data we hold; it is never an observation, and
    #: an answer attributed to "the AI" is not something this platform prints.
    provenance: ClassVar[DataProvenance] = DataProvenance.DERIVED

    @abc.abstractmethod
    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Send a conversation and return what the model produced.

        ``tools`` are JSON-schema tool definitions. Building them, and validating
        whatever the model asks for against them, belongs to the dispatcher — a
        provider passes them through and reports back what was requested.
        """


class OllamaLLMProvider(LLMProvider):
    """A model served by Ollama on the host. The intended deployment.

    Local inference is a requirement, not a cost saving: shipment routes, incident
    reports and vehicle positions for a strategically sensitive region stay on the
    operator's hardware. That property is worth stating in the provenance panel,
    which is why :attr:`local_only` is on the class rather than in a comment.

    The model tag is configuration, and :meth:`healthcheck` checks the configured
    tag is actually pulled. "Model not found" is the most common setup failure
    with Ollama and it otherwise appears as a 500 on the first question asked.
    """

    implementation: ClassVar[str] = "ollama"
    source_name: ClassVar[str] = "ollama"
    local_only: ClassVar[bool] = True

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 120.0,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._client: Any = None

    @property
    def model(self) -> str:
        return self._model

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _tool_calls(raw: Any) -> List[LLMToolCall]:
        """Convert Ollama's ``tool_calls`` into our request objects.

        A call whose arguments cannot be parsed is dropped, not repaired. Passing
        a half-understood request to the dispatcher is how a malformed model
        output turns into an action nobody asked for; a dropped call simply means
        the assistant did not manage to ask, which the user can see and retry.
        """

        calls: List[LLMToolCall] = []
        if not isinstance(raw, list):
            return calls
        for item in raw:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    continue
            if not isinstance(arguments, dict):
                continue
            calls.append(LLMToolCall(name=name, arguments=arguments))
        return calls

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        if not messages:
            raise ValueError("complete() needs at least one message")
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                "temperature": self._temperature if temperature is None else temperature,
                "num_predict": self._max_tokens if max_tokens is None else max_tokens,
            },
        }
        if tools:
            payload["tools"] = list(tools)

        client = self._http()
        try:
            response = await client.post(f"{self._base_url}/api/chat", json=payload)
        except Exception as exc:  # httpx is imported lazily
            raise ProviderUnavailable(
                f"{self.name}: cannot reach Ollama at {self._base_url}: {exc}"
            ) from exc
        if response.status_code == 404:
            raise ProviderResponseError(
                f"{self.name}: Ollama does not have model {self._model!r} "
                "(pull it, or set LLM_MODEL to a tag from `ollama list`)"
            )
        if response.status_code >= 400:
            raise ProviderResponseError(f"{self.name}: HTTP {response.status_code} from Ollama")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseError(f"{self.name}: non-JSON response") from exc

        message = body.get("message")
        message = message if isinstance(message, dict) else {}
        usage = {
            key: body[key]
            for key in ("prompt_eval_count", "eval_count", "total_duration", "eval_duration")
            if key in body
        }
        return LLMResponse(
            text=str(message.get("content") or ""),
            model=str(body.get("model") or self._model),
            tool_calls=self._tool_calls(message.get("tool_calls")),
            finish_reason=str(body.get("done_reason")) if body.get("done_reason") else None,
            usage=usage or None,
        )

    async def healthcheck(self) -> ProviderStatus:
        """Lists local models and reports whether the configured tag is present."""

        try:
            client = self._http()
            response = await client.get(f"{self._base_url}/api/tags", timeout=5.0)
            if response.status_code >= 400:
                return self.describe(
                    healthy=False, detail=f"HTTP {response.status_code} from {self._base_url}"
                )
            models = response.json().get("models", [])
            tags = {str(m.get("name")) for m in models if isinstance(m, dict)}
        except Exception as exc:  # noqa: BLE001 - a health probe must never raise
            return self.describe(healthy=False, detail=f"unreachable: {exc}")
        if self._model in tags:
            return self.describe(healthy=True, detail=f"{self._model} available locally")
        return self.describe(
            healthy=False,
            detail=f"{self._model} not pulled; available: {', '.join(sorted(tags)) or 'none'}",
        )


class StubLLMProvider(LLMProvider):
    """Answers nothing, deliberately. For tests and for demos without a model.

    It would be easy to make this return canned prose so a demo looks complete.
    That is precisely the failure this project avoids: a scripted answer presented
    as the assistant's reasoning is a fabricated capability. So it returns one
    sentence saying the assistant is not running, and no tool calls.
    """

    implementation: ClassVar[str] = "stub"
    source_name: ClassVar[str] = "stub"
    message: ClassVar[str] = (
        "The assistant is not connected to a language model in this deployment, so "
        "it cannot answer. Every figure it would have summarised is available "
        "directly from the risk, routing and incident views."
    )

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        return LLMResponse(text=self.message, model="stub", finish_reason="not_configured")

    async def healthcheck(self) -> ProviderStatus:
        return self.describe(healthy=True, detail="stub: returns a fixed refusal, never content")


class DisabledLLMProvider(UnconfiguredProvider, LLMProvider):
    """What the registry returns when ``LLM_ENABLED`` is false — the default.

    Distinct from :class:`StubLLMProvider`, which answers with a refusal: this
    raises, so the assistant endpoints can return 503 and be visibly off rather
    than appearing to work and having nothing to say.
    """

    implementation: ClassVar[str] = "disabled"
    reason: ClassVar[str] = "LLM_ENABLED is false; the assistant is switched off"

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        raise self._unavailable()


class OpenAICompatibleLLMProvider(UnconfiguredProvider, LLMProvider):
    """A hosted OpenAI-compatible endpoint. Declared, not connected.

    The interface is here so a deployment with its own approved inference service
    can use it. It is worth being explicit about what switching to it means:
    operational data for this region would leave the operator's hardware and cross
    a network to a third party. That is a decision for whoever runs the system,
    made deliberately by supplying a base URL and a key — not a default.
    """

    implementation: ClassVar[str] = "openai_compatible"
    reason: ClassVar[str] = (
        "no base URL or API key configured; enabling it sends operational data "
        "off-host, which is a deployment decision"
    )

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        raise self._unavailable()
