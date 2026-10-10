"""The one-line cost/latency footer under a message the harness posts.

Every number here was already measured and thrown away — RunResult has carried tokens,
cache, and cost since the beginning, and state.json accumulated totals nobody read. The
only new thing is that Slack shows them per message.

Two rules this module exists to keep:

1. A footer is rendered ONLY from a real run. A deterministic post never touched the
   API, so a footer under it claiming 0 tokens would be a lie in the shape of a
   measurement — `format_run_footer` returns "" and the caller appends nothing.
2. Numbers are rendered, never recomputed. A missing field is omitted, not defaulted to
   zero: an absent cost and a genuinely free call must not look alike. This is also why
   a Codex footer is simply shorter — that provider reports no billed model and no USD.
"""

from __future__ import annotations


def _tokens(n: int) -> str:
    """12400 -> 12.4k. Exact under 1000, because 'how many tools' lives on the same line
    and 0.9k reads as a rounding of something bigger."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k".replace(".0k", "k")


def format_run_footer(last: dict | None) -> str:
    """The footer TEXT for one run, or "" when there is nothing truthful to report.

    `last` is the state["agents"][<key>]["last"] snapshot written by record_result_usage
    — a plain dict, so this renders identically whether the caller holds a live
    RunResult (mentions) or is reading it back out of state (announce)."""
    if not last:
        return ""
    # ⬆ is EVERYTHING the model read, not `input_tokens`. That SDK field is uncached
    # input only, and on a real run it is a rounding error: input_tokens=12 against
    # cache_creation=23283 + cache_read=93605 would render "⬆ 12" for a call that read
    # 116,900 tokens. A footer exists to be believed at a glance, so ⬆ sums all three
    # and 💾 says how much of it was cheap. Do not "fix" this back to the raw field.
    fresh = int(last.get("input_tokens") or 0)
    write = int(last.get("cache_creation_tokens") or 0)
    cached = int(last.get("cache_read_tokens") or 0)
    inp = fresh + write + cached
    out = int(last.get("output_tokens") or 0)
    # No tokens either way means no API call stands behind this message.
    if not inp and not out:
        return ""

    bits = []
    if last.get("duration_s"):
        bits.append(f"⏱ {float(last['duration_s']):.0f}s")
    if last.get("model"):
        bits.append(str(last["model"]))
    bits.append(f"⬆ {_tokens(inp)} ⬇ {_tokens(out)}")
    if cached:
        bits.append(f"💾 {_tokens(cached)} cached ({cached * 100 // inp}%)")
    tools = int(last.get("tools_available") or 0)
    if tools:
        bits.append(f"🧰 {tools} tool{'s' if tools != 1 else ''}")
    cost = float(last.get("cost_usd") or 0.0)
    if cost:
        # 4dp under a cent: a haiku mention reply lands around $0.0007, and "$0.00" for a
        # call that genuinely cost money is the same lie as a zeroed footer.
        bits.append(f"${cost:.4f}".rstrip("0").rstrip(".") if cost < 0.01 else f"${cost:.3f}")
    elif last.get("provider") == "codex":
        bits.append("USD 미제공")

    return " · ".join(bits)


def footer_context(last: dict | None) -> dict | None:
    """`format_run_footer` as a Slack context block, or None when there is nothing.

    A context block renders small and grey UNDER the message rather than inside it, so it
    annotates the finding instead of competing with it (a rule + italics in the body did
    compete, and was replaced by this)."""
    line = format_run_footer(last)
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": line}]} if line else None


def run_fields(result) -> dict | None:
    """A RunResult as the dict the footer reads. Both footer sites hold a RunResult;
    passing it straight to `footer_context` failed on `.get` and took the task
    announcement down with it."""
    if result is None:
        return None
    return {
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_creation_tokens": result.cache_creation_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cost_usd": result.cost_usd,
        "duration_s": result.duration_s,
        "model": result.model,
        "tools_available": result.tools_available,
        "provider": result.provider,
    }


__all__ = ["footer_context", "format_run_footer", "run_fields"]
