"""EFF-03a tests: exactly-once retry-without-param on provider effort 400s.

Verifies:
1. A 400 mentioning reasoning/effort WITH an effort param sent → ONE silent
   retry without the param succeeds (traced via warning log).
2. Exactly-once: a second 400 propagates (no param-strip loop).
3. A 400 NOT mentioning reasoning/effort → propagates immediately (no retry).
4. A non-400 (e.g. 500) with an effort param → propagates (not an effort
   rejection).
5. OpenRouter shape: ``extra_body.reasoning`` is stripped the same way.
"""

from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged


def _effort_400():
    exc = Exception(
        "400 Bad Request: unknown parameter: reasoning_effort is not supported by this model"
    )
    exc.status_code = 400
    return exc


@tagged("post_install", "-at_install")
class TestEffortRetry(TransactionCase):
    """EFF-03a: retry-without-param acceptance matrix."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Scaleway Effort-Retry Test",
                    "service": "openai",
                    "api_base": "https://api.scaleway.ai/v1",
                    "api_key": "sk-test-key",
                },
            )
        )
        cls.model = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "gpt-oss-120b",
                    "provider_id": cls.provider.id,
                    "model_use": "chat",
                    "reasoning_effort": "none",
                },
            )
        )
        cls.provider_or = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "OpenRouter Effort-Retry Test",
                    "service": "openai",
                    "api_base": "https://openrouter.ai/api/v1",
                    "api_key": "sk-test-key",
                },
            )
        )
        cls.model_or = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "z-ai/glm-5.2",
                    "provider_id": cls.provider_or.id,
                    "model_use": "chat",
                    "reasoning_effort": "low",
                },
            )
        )

    def _mock_client_rejecting_effort(self, fail_times=1, always_fail=False):
        """Client whose create() 400s on the effort param `fail_times`, then
        returns a valid non-streaming response. With ``always_fail``, every
        call raises (the retry's stripped call fails too)."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "ok"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].message.reasoning_content = None
        mock_response.usage = None
        state = {"calls": 0}

        def _create(**kwargs):
            state["calls"] += 1
            has_effort = "reasoning_effort" in kwargs or "reasoning" in (
                kwargs.get("extra_body") or {}
            )
            if always_fail or (has_effort and state["calls"] <= fail_times):
                raise _effort_400()
            return mock_response

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _create
        return mock_client, state

    def test_400_on_effort_retries_once_without_param(self):
        """gpt-oss-120b with effort=none: first call 400s, retry succeeds
        WITHOUT the param — exactly one retry, silent for the caller."""
        mock_client, state = self._mock_client_rejecting_effort(fail_times=1)
        with patch.object(type(self.provider), "client", mock_client):
            result = self.provider.openai_chat(
                messages=self.env["mail.message"],
                model=self.model,
                stream=False,
            )
        self.assertEqual(result.get("content"), "ok", "retry without param succeeds")
        self.assertEqual(state["calls"], 2, "exactly one retry")
        second_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("reasoning_effort", second_kwargs, "param stripped on retry")
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        self.assertEqual(
            first_kwargs.get("reasoning_effort"), "none", "effort sent on first call"
        )

    def test_second_400_propagates_exactly_once(self):
        """Both calls 400 → the error propagates after exactly 2 calls
        (no param-strip loop)."""
        mock_client, state = self._mock_client_rejecting_effort(always_fail=True)
        raised = False
        with patch.object(type(self.provider), "client", mock_client):
            try:
                self.provider.openai_chat(
                    messages=self.env["mail.message"],
                    model=self.model,
                    stream=False,
                )
            except Exception:
                raised = True
        self.assertTrue(raised, "persistent 400 propagates")
        self.assertEqual(state["calls"], 2, "exactly one retry, then propagate")

    def test_unrelated_400_not_retried(self):
        """A 400 NOT mentioning reasoning/effort → immediate raise, 1 call."""
        exc = Exception("400 Bad Request: messages field is invalid")
        exc.status_code = 400
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = exc
        raised = False
        with patch.object(type(self.provider), "client", mock_client):
            try:
                self.provider.openai_chat(
                    messages=self.env["mail.message"],
                    model=self.model,
                    stream=False,
                )
            except Exception:
                raised = True
        self.assertTrue(raised)
        self.assertEqual(
            mock_client.chat.completions.create.call_count,
            1,
            "no retry for unrelated 400",
        )

    def test_500_not_an_effort_rejection(self):
        """A 500 with an effort param → not an effort rejection → 1 call."""
        exc = Exception("500 Internal Server Error")
        exc.status_code = 500
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = exc
        raised = False
        with patch.object(type(self.provider), "client", mock_client):
            try:
                self.provider.openai_chat(
                    messages=self.env["mail.message"],
                    model=self.model,
                    stream=False,
                )
            except Exception:
                raised = True
        self.assertTrue(raised)
        self.assertEqual(
            mock_client.chat.completions.create.call_count,
            1,
            "500s are not retried here",
        )

    def test_openrouter_reasoning_dict_stripped(self):
        """OpenRouter shape: extra_body.reasoning is stripped on retry."""
        mock_client, state = self._mock_client_rejecting_effort(fail_times=1)
        with patch.object(type(self.provider_or), "client", mock_client):
            result = self.provider_or.openai_chat(
                messages=self.env["mail.message"],
                model=self.model_or,
                stream=False,
            )
        self.assertEqual(result.get("content"), "ok")
        self.assertEqual(state["calls"], 2)
        first_kwargs = mock_client.chat.completions.create.call_args_list[0].kwargs
        self.assertEqual(
            (first_kwargs.get("extra_body") or {}).get("reasoning"), {"effort": "low"}
        )
        second_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn(
            "reasoning",
            second_kwargs.get("extra_body") or {},
            "reasoning dict stripped",
        )

    def test_simple_completion_retries_without_param(self):
        """The title/simple-completion path gets the same exactly-once retry."""
        mock_client, state = self._mock_client_rejecting_effort(fail_times=1)
        with patch.object(type(self.provider), "client", mock_client):
            result = self.provider.openai_simple_completion(
                "Say hi",
                model=self.model,
                reasoning_effort="none",
                max_tokens=10,
            )
        self.assertEqual(result, "ok")
        self.assertEqual(state["calls"], 2)
        second_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("reasoning_effort", second_kwargs)
