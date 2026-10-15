import pytest

from bot_telegram_alerts import (
    MAX_MESSAGE,
    TelegramAlerter,
    TelegramError,
    escape_html,
    escape_markdown_v2,
    split_message,
)

TOKEN = "123456789:AAFakeTokenForTestsFakeTokenForTes"


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return 200, {"ok": True}


def make(transport=None, **kw):
    clock = Clock()
    transport = transport or FakeTransport()
    alerter = TelegramAlerter(
        TOKEN, 42, transport=transport, clock=clock.now, sleep=clock.sleep, **kw
    )
    return alerter, transport, clock


def test_sends_to_the_right_endpoint_and_chat():
    a, t, _ = make()
    assert a.send("hello").sent
    url, payload = t.calls[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert payload["chat_id"] == 42 and payload["text"] == "hello"


def test_duplicates_are_suppressed_then_reported():
    a, t, clock = make(dedup_window=60)
    assert a.send("disk full").sent
    assert a.send("disk full").reason == "duplicate"
    assert a.send("disk full").reason == "duplicate"
    assert len(t.calls) == 1
    clock.t += 61
    assert a.send("disk full").sent
    assert t.calls[1][1]["text"].endswith("(+2 repeated)")
    assert a.stats.duplicates == 2


def test_explicit_key_groups_different_texts():
    a, _t, _ = make()
    a.send("latency 120ms", key="latency")
    assert a.send("latency 340ms", key="latency").reason == "duplicate"
    assert a.send("other alert").sent


def test_rate_limit_drops_excess():
    a, _t, clock = make(max_per_minute=2)
    assert a.send("a").sent and a.send("b").sent
    assert a.send("c").reason == "rate_limited"
    clock.t += 61
    assert a.send("d").sent
    assert a.stats.rate_limited == 1


def test_rate_limit_wait_mode_sleeps_instead_of_dropping():
    a, _t, clock = make(max_per_minute=1, on_limit="wait")
    a.send("a")
    assert a.send("b").sent
    assert clock.sleeps and clock.sleeps[0] == pytest.approx(60)


def test_retries_429_honouring_retry_after():
    t = FakeTransport([(429, {"parameters": {"retry_after": 7}}), (200, {"ok": True})])
    a, _, clock = make(t)
    assert a.send("x").sent
    assert clock.sleeps == [7.0] and a.stats.retries == 1


def test_retries_5xx_and_network_errors_with_backoff():
    t = FakeTransport([(502, {}), OSError("connection reset"), (200, {"ok": True})])
    a, _, clock = make(t)
    assert a.send("x").sent
    assert clock.sleeps == [1.0, 2.0]


def test_4xx_is_not_retried_and_raises_without_token():
    t = FakeTransport([(400, {"description": "Bad Request: chat not found"})])
    a, _, _ = make(t)
    with pytest.raises(TelegramError) as err:
        a.send("x")
    assert "chat not found" in str(err.value) and TOKEN not in str(err.value)
    assert len(t.calls) == 1


def test_token_is_scrubbed_from_network_errors_and_repr():
    t = FakeTransport([OSError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage")] * 4)
    a, _, _ = make(t, max_retries=3)
    with pytest.raises(TelegramError) as err:
        a.send("x")
    assert TOKEN not in str(err.value)
    assert TOKEN not in repr(a)


def test_send_safe_never_raises():
    a, _, _ = make(FakeTransport([(400, {"description": "nope"})]))
    r = a.send_safe("x")
    assert not r.sent and r.reason == "failed"


def test_long_message_is_split_and_all_chunks_sent():
    text = "\n".join(f"line {i:04d} " + "x" * 90 for i in range(120))
    a, t, _ = make()
    r = a.send(text)
    assert r.chunks == len(t.calls) > 1
    assert all(len(c[1]["text"]) <= MAX_MESSAGE for c in t.calls)
    assert "\n".join(c[1]["text"] for c in t.calls) == text


def test_split_message_hard_cuts_a_giant_line():
    chunks = split_message("y" * 10_000, 4096)
    assert [len(c) for c in chunks] == [4096, 4096, 1808]
    assert split_message("short") == ["short"]


def test_parse_mode_and_silent_flags_are_forwarded():
    a, t, _ = make()
    a.send("<b>x</b>", parse_mode="HTML", silent=True)
    p = t.calls[0][1]
    assert p["parse_mode"] == "HTML" and p["disable_notification"] is True


def test_escaping_helpers():
    assert escape_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert escape_markdown_v2("1.5 - x_y!") == r"1\.5 \- x\_y\!"


def test_requires_credentials():
    with pytest.raises(ValueError):
        TelegramAlerter("", 1)
    with pytest.raises(ValueError):
        TelegramAlerter(TOKEN, 1, on_limit="explode")
