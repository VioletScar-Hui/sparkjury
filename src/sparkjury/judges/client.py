"""Judge implementations.

- `OpenAICompatJudge`: any OpenAI-compatible chat endpoint (vLLM on DGX, StepFun, OpenRouter).
- `MockJudge`: deterministic heuristics over the trace. Used for offline tests and as the
  degradation path when no backend is reachable. Slight per-judge jitter is applied so a mock
  panel still produces occasional disagreements for the arbitration path to exercise.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Protocol

from sparkjury.judges import heuristics
from sparkjury.judges.prompts import build_messages, parse_verdict_json
from sparkjury.models.trace import Trace
from sparkjury.models.verdict import Dimension, Verdict


class Judge(Protocol):
    name: str
    model: str

    def score(self, trace: Trace, dimension: Dimension) -> Verdict: ...


# ---- LLM judge ---------------------------------------------------------------


class OpenAICompatJudge:
    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        max_tokens: int = 600,
        extra_body: dict | None = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install openai (or `uv sync`) to use OpenAICompatJudge") from e
        self.name = name
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self._client = OpenAI(base_url=base_url, api_key=api_key or os.environ.get("OPENAI_API_KEY") or "EMPTY",
                              timeout=timeout_s, max_retries=max_retries)

    def healthcheck(self, timeout_s: float = 3.0) -> bool:
        """True when the endpoint answers /models (vLLM, StepFun and OpenRouter all do).

        失败原因存进 self.health_error：401（key 无效）和连接超时（网络不通）在降级
        消息里长得一样，排查方向却完全相反——DGX 节点上实测过 StepFun key 无效被
        误当网络问题查了一轮。"""
        try:
            self._client.with_options(timeout=timeout_s, max_retries=0).models.list()
            self.health_error: str | None = None
            return True
        except Exception as e:  # noqa: BLE001
            self.health_error = f"{type(e).__name__}: {e}"
            return False

    def score(self, trace: Trace, dimension: Dimension) -> Verdict:
        messages = build_messages(trace, dimension)
        t0 = time.perf_counter()
        raw: str | None = None
        try:
            raw = self._chat(messages)
            data = parse_verdict_json(raw)
        except Exception as e:  # noqa: BLE001 - any backend/parse failure becomes an errored verdict
            # one nudge retry for malformed JSON
            if raw is not None:
                try:
                    raw2 = self._chat(messages + [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": "Output only the JSON object described in the instructions."},
                    ])
                    data = parse_verdict_json(raw2)
                    raw = raw2
                except Exception as e2:  # noqa: BLE001
                    return self._errored(trace, dimension, f"{type(e2).__name__}: {e2}", raw, t0)
            else:
                return self._errored(trace, dimension, f"{type(e).__name__}: {e}", raw, t0)
        if dimension == Dimension.OUTCOME and data["label"] is None:
            data["label"] = "pass" if data["score"] >= 3 else "fail"
        if dimension != Dimension.OUTCOME:
            data["label"] = None
        return Verdict(
            trace_id=trace.trace_id, judge=self.name, model=self.model, dimension=dimension,
            latency_ms=(time.perf_counter() - t0) * 1000, raw=raw, **data,
        )

    def _chat(self, messages: list[dict[str, str]]) -> str:
        resp = self._client.chat.completions.create(
            model=self.model, messages=messages, temperature=self.temperature,
            max_tokens=self.max_tokens, extra_body=self.extra_body or None,
        )
        return resp.choices[0].message.content or ""

    def _errored(self, trace: Trace, dimension: Dimension, err: str, raw: str | None, t0: float) -> Verdict:
        return Verdict(trace_id=trace.trace_id, judge=self.name, model=self.model, dimension=dimension,
                       error=err, raw=raw, latency_ms=(time.perf_counter() - t0) * 1000)


# ---- Mock judge --------------------------------------------------------------


class MockJudge:
    """Heuristic judge. `jitter` adds deterministic per-(judge, trace) noise in [0, 1] score points."""

    def __init__(self, name: str = "mock", model: str = "mock-heuristic-v1", *, jitter: float = 0.0, latency_ms: float = 0.0):
        self.name = name
        self.model = model
        self.jitter = jitter
        self.latency_ms = latency_ms

    def score(self, trace: Trace, dimension: Dimension) -> Verdict:
        t0 = time.perf_counter()
        base = heuristics.evaluate(trace, dimension)
        score, label = base.score, base.label
        if self.jitter > 0:
            h = int(hashlib.sha1(f"{self.name}|{trace.trace_id}|{dimension.value}".encode()).hexdigest(), 16)
            u = (h % 1000) / 1000.0
            if u < self.jitter:
                delta = 1 + (1 if (h >> 12) % 4 == 0 else 0)   # mostly +-1, sometimes +-2
                score = max(0, min(4, score + (delta if (h >> 10) % 2 else -delta)))
                if dimension == Dimension.OUTCOME and trace.outcome.success is None:
                    label = "pass" if score >= 3 else "fail"
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        return Verdict(
            trace_id=trace.trace_id, judge=self.name, model=self.model, dimension=dimension,
            score=score, label=label if dimension == Dimension.OUTCOME else None,
            confidence=base.confidence, evidence_steps=base.evidence_steps, rationale=base.rationale,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
