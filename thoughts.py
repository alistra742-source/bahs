"""The writer's stream, with its thinking kept.

`bridge.stream_call` reads one answer out of a provider's `/chat/completions` and drops
`reasoning_content` on the way past: the answer is the script, and the reasoning about it is not the
script. That is right for the answer and wrong for a client that wants to watch the writer think --
the Roblox client's THINKING pane, which it fills from `/chat/result` while the turn runs.

So this module carries the same stream with one addition: every reasoning fragment goes to the
box's `thoughts` callback as it arrives -- that is what makes a pane move while the model is still
deciding -- and the whole chain of thought is left in `box["thought_text"]` when the call ends. A
box without the callback behaves exactly as it does on the bridge's version: the reasoning is
collected and then dropped.

It is a copy of `bridge.stream_call` rather than a wrapper around it, because the fragment has to
be caught inside the loop that reads the provider's frames and there is no seam outside it. A
change to `bridge.stream_call` has to be made here too.
"""
import json
from typing import Optional

import httpx
from fastapi import HTTPException

from bridge import (Cutter, META_RE, Provider, client_timeout, failure_reason, message_text,
                    strip_metadata, tool_calls_of, upstream_error, _merge_call)

# Where a provider puts the model's own chain of thought. OpenAI-shaped endpoints call it
# reasoning_content; some dialects shorten it to reasoning.
THOUGHT_KEYS = ("reasoning_content", "reasoning")


def thought_fragment(delta: dict) -> str:
    """The reasoning one delta carries, if it carries any.

    Kept apart from the answer on purpose: a fragment must never reach the caller as content, or
    it lands in the middle of the script.
    """
    for key in THOUGHT_KEYS:
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def stream_with_thoughts(messages: list, temperature: Optional[float], provider: Provider,
                         max_tokens: int, box: Optional[dict] = None,
                         tools: Optional[list] = None, tool_choice: Optional[str] = None):
    """Stream one answer, thinking included.

    Yields exactly what `bridge.stream_call` yields: the answer, with the tool XML and the
    continuation metadata cut out. What it adds is the reasoning -- live on the box's `thoughts`
    callback, and complete in `box["thought_text"]` afterwards.
    """
    body = provider.request(messages, temperature, max_tokens, stream=True,
                            tools=tools, tool_choice=tool_choice)
    cutter = Cutter()
    streamed_calls: list = []
    thinking: list = []

    def keep(fragment: str) -> None:
        thinking.append(fragment)
        if box is not None and callable(box.get("thoughts")):
            box["thoughts"](fragment)

    try:
        with httpx.Client(timeout=client_timeout(provider.timeout),
                          follow_redirects=True) as c:
            with c.stream("POST", provider.endpoint(), json=body, headers=provider.headers()) as r:
                if r.status_code >= 400:
                    detail = r.read().decode("utf-8", "replace")
                    raise HTTPException(502, failure_reason(r.status_code, detail, provider))
                if "event-stream" not in r.headers.get("content-type", ""):
                    # Not a stream: either the endpoint rejected the request with a 200, or it
                    # ignored stream=true and answered in one piece. Reading the body tells us
                    # which; reporting an empty answer would hide the reason.
                    raw = r.read().decode("utf-8", "replace")
                    text = message_text(raw)
                    if not text:
                        raise HTTPException(502, failure_reason(200, raw, provider))
                    print(f"[{provider.name}] answered in one piece instead of streaming", flush=True)
                    if box is not None:
                        box["finish"] = box.get("finish") or "stop"
                        # A one-piece answer carries no deltas at all, so it carries no thinking
                        # either -- an empty string rather than a missing key, because the model
                        # says the same thing by having produced none.
                        box["thought_text"] = ""
                    visible = strip_metadata(text)
                    if box is not None:
                        box["raw"] = text
                    yield visible
                    return
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if box is not None and chunk.get("usage"):
                        box["usage"] = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] or {}
                    if box is not None and choice.get("finish_reason"):
                        box["finish"] = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    for call in delta.get("tool_calls") or []:
                        _merge_call(streamed_calls, call)
                    thought = thought_fragment(delta)
                    if thought:
                        keep(thought)
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        shown = cutter.feed(piece)
                        if shown:
                            yield shown
    except httpx.HTTPError as e:
        raise upstream_error(e, provider)
    if box is not None:
        box["thought_text"] = "".join(thinking)
        box["raw"] = cutter.raw
        box["meta"] = " ".join(META_RE.findall(cutter.raw)).strip()
        box["tool_calls"] = tool_calls_of(cutter.raw, streamed_calls)
