"""LLM interface and providers.

`LLMClient.complete_json` returns a validated Pydantic object; `complete_text` returns text.
The provider is chosen by LLM_PROVIDER:
  - anthropic: Claude via the official Anthropic SDK (structured JSON outputs).
  - fake: deterministic offline stub for local runs and tests (no network, no key).
"""

from __future__ import annotations

import abc
import asyncio
import contextvars
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

import anthropic
import structlog
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)

INPUT_OPEN, INPUT_CLOSE = "<input>", "</input>"


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMUnavailable(LLMError):
    """The provider could not be reached, rate-limited us past retries, or rejected the request."""


class LLMOutputError(LLMError):
    """The model's output did not match the schema, even after one corrective retry."""


# ------------------------------------------------------------- token usage
_usage: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar("llm_usage", default=None)


@contextmanager
def track_usage() -> Iterator[dict[str, int]]:
    """Collect token usage of every LLM call made in this context (one job = one report)."""
    totals: dict[str, int] = {}
    token = _usage.set(totals)
    try:
        yield totals
    finally:
        _usage.reset(token)


def _record_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    totals = _usage.get()
    if totals is None:
        return
    for key, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        (f"{model}:input_tokens", input_tokens),
        (f"{model}:output_tokens", output_tokens),
        ("calls", 1),
    ):
        totals[key] = totals.get(key, 0) + value


def embed_input(payload: Any) -> str:
    """Put structured task data in the prompt between <input> tags."""
    return f"{INPUT_OPEN}\n{json.dumps(payload, ensure_ascii=False, indent=1, default=str)}\n{INPUT_CLOSE}"


def extract_input(prompt: str) -> Any:
    start, end = prompt.find(INPUT_OPEN), prompt.rfind(INPUT_CLOSE)
    if start < 0 or end < 0:
        return None
    return json.loads(prompt[start + len(INPUT_OPEN) : end])


# ------------------------------------------------------------- JSON schema
_UNSUPPORTED_KEYS = {
    "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "maxItems", "pattern", "format", "default", "title", "examples",
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> structured-output schema: every object closed, every property required.

    Length/range constraints are removed here and enforced by Pydantic validation instead.
    """

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
            if "minItems" in out and out["minItems"] > 1:
                out.pop("minItems")
            if out.get("type") == "object" and "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return clean(model.model_json_schema())


# ------------------------------------------------------------- interface
class LLMClient(abc.ABC):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.LLM_MAX_CONCURRENCY)

    @property
    def default_model(self) -> str:
        return self.settings.LLM_DEFAULT_MODEL

    async def complete_json(
        self, system: str, user: str, schema: type[T], model: str | None = None, *, max_tokens: int | None = None
    ) -> T:
        model = model or self.default_model
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        text = await self._call(system, messages, model, max_tokens=max_tokens, schema=schema)
        try:
            return schema.model_validate_json(text)
        except ValidationError as first_error:
            log.info("llm_json_invalid_retrying", model=model, schema=schema.__name__, errors=first_error.error_count())
            messages += [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "Your previous output failed validation:\n"
                    f"{first_error}\nReturn the complete corrected JSON object only.",
                },
            ]
            text = await self._call(system, messages, model, max_tokens=max_tokens, schema=schema)
            try:
                return schema.model_validate_json(text)
            except ValidationError as exc:
                raise LLMOutputError(f"{schema.__name__} output invalid after retry: {exc.error_count()} errors") from exc

    async def complete_text(self, system: str, user: str, model: str | None = None, *, max_tokens: int | None = None) -> str:
        model = model or self.default_model
        return await self._call(system, [{"role": "user", "content": user}], model, max_tokens=max_tokens, schema=None)

    async def _call(
        self,
        system: str,
        messages: list[dict[str, Any]],
        model: str,
        *,
        max_tokens: int | None,
        schema: type[BaseModel] | None,
    ) -> str:
        async with self._semaphore:
            return await self._complete(system, messages, model, max_tokens or self.settings.LLM_MAX_TOKENS, schema)

    @abc.abstractmethod
    async def _complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        schema: type[BaseModel] | None,
    ) -> str: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass


# ------------------------------------------------------------- Anthropic
def _model_family(model: str) -> str:
    if model.startswith("claude-haiku-4-5"):
        return "haiku-4-5"
    return "current"


class AnthropicLLMClient(LLMClient):
    """Claude via the Anthropic SDK.

    The SDK retries connection errors, 408/409/429 and 5xx with exponential backoff
    (LLM_MAX_RETRIES times); after that the error surfaces as LLMUnavailable.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"
    FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        super().__init__(settings)
        api_key = settings.ANTHROPIC_API_KEY.get_secret_value() if settings.ANTHROPIC_API_KEY else None
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=settings.LLM_MAX_RETRIES
        )

    def build_request(
        self, system: str, messages: list[dict[str, Any]], model: str, max_tokens: int, schema: type[BaseModel] | None
    ) -> dict[str, Any]:
        request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        output_config: dict[str, Any] = {}
        if _model_family(model) == "haiku-4-5":
            # Haiku 4.5 accepts sampling parameters but not `effort`. SDK 1.x has no typed
            # `temperature` argument (current models reject it), so send it in the body.
            request["extra_body"] = {"temperature": self.settings.LLM_TEMPERATURE}
        else:
            # Current models reject sampling parameters; thinking is adaptive by default,
            # so depth is controlled with effort.
            output_config["effort"] = self.settings.LLM_EFFORT
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(schema)}
        if output_config:
            request["output_config"] = output_config
        if model.startswith(self.FALLBACK_MODELS):
            request["betas"] = [self.FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    async def _complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        schema: type[BaseModel] | None,
    ) -> str:
        request = self.build_request(system, messages, model, max_tokens, schema)
        try:
            # Streaming avoids HTTP timeouts on long generations; we only need the final message.
            async with self._client.beta.messages.stream(**request) as stream:
                response = await stream.get_final_message()
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LLMUnavailable(f"LLM connection failed: {type(exc).__name__}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable("LLM rate limit exceeded after retries") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable("LLM authentication failed; check ANTHROPIC_API_KEY") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMUnavailable("LLM permission denied for this model") from exc
        except anthropic.BadRequestError as exc:
            raise LLMUnavailable(f"LLM rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"LLM error (HTTP {exc.status_code})") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            _record_usage(model, int(usage.input_tokens or 0), int(usage.output_tokens or 0))
        if response.stop_reason == "refusal":
            raise LLMUnavailable("The model declined this request")
        if response.stop_reason == "max_tokens":
            # Truncated JSON fails validation and gets one corrective retry in complete_json.
            log.warning("llm_output_truncated", model=model, max_tokens=max_tokens)
        return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


# ------------------------------------------------------------- Fake
FakeHandler = Callable[[str, Any], BaseModel | str]


class FakeLLMClient(LLMClient):
    """Deterministic offline LLM. Handlers receive (system prompt, parsed <input> payload).

    Pipeline stages register default handlers in app.pipeline.fake_handlers; tests can
    override any handler with `set_handler`.
    """

    def __init__(self, settings: Settings, handlers: dict[str, FakeHandler] | None = None) -> None:
        super().__init__(settings)
        from app.pipeline.fake_handlers import DEFAULT_HANDLERS

        self.handlers: dict[str, FakeHandler] = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.calls: list[dict[str, Any]] = []

    def set_handler(self, name: str, handler: FakeHandler) -> None:
        self.handlers[name] = handler

    async def _complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int,
        schema: type[BaseModel] | None,
    ) -> str:
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[0]["content"]
        original = messages[0]["content"]
        task = re.search(r"TASK:\s*([a-z_]+)", system)
        name = task.group(1) if task else (schema.__name__ if schema else "text")
        self.calls.append({"task": name, "model": model, "retry": len(messages) > 1, "user": user})
        handler = self.handlers.get(name)
        if handler is None:
            raise LLMUnavailable(f"No fake handler for task {name!r}")
        result = handler(system, extract_input(original))
        _record_usage(model, len(original) // 4, 50)
        if isinstance(result, BaseModel):
            return result.model_dump_json()
        return result


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.LLM_PROVIDER == "fake":
        return FakeLLMClient(settings)
    return AnthropicLLMClient(settings)
