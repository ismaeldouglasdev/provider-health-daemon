import pytest

from daily_usage import daily_provider_usage

FIXTURE = "\n".join(
    [
        "[10:00:01] 🔴 ▶ POST groq/llama-3.3-70b-versatile → groq/llama-3.3-70b-versatile · FMT: openai→groq · JSON · 2 MSG · ACC:groq",
        "[10:00:05] 📊 DONE 1200ms · IN 500 (CACHE ↻100) · OUT 250",
        "[10:01:01] 🔴 ▶ POST samba/Meta-Llama-3.3-70B-Instruct → samba/Meta-Llama-3.3-70B-Instruct · FMT: openai→samba · JSON · 1 MSG · ACC:samba",
        "[10:01:04] 📊 DONE 900ms · TTFT 200ms · IN 300 · OUT 150",
        "[10:02:01] 🔴 ▶ POST groq/llama-3.3-70b-versatile → groq/llama-3.3-70b-versatile · FMT: openai→groq · JSON · 1 MSG · ACC:groq",
    ]
)


def test_sums_today_by_provider(tmp_path):
    log = tmp_path / "access.log"
    log.write_text(FIXTURE, encoding="utf-8")
    result = daily_provider_usage(log_path=str(log))
    assert result == {
        "groq": {"tokens": 850, "requests": 2},
        "samba": {"tokens": 450, "requests": 1},
    }


def test_empty_log_returns_empty_dict(tmp_path):
    log = tmp_path / "access.log"
    log.write_text("", encoding="utf-8")
    assert daily_provider_usage(log_path=str(log)) == {}


def test_past_date_returns_empty(tmp_path):
    log = tmp_path / "access.log"
    log.write_text(FIXTURE, encoding="utf-8")
    assert daily_provider_usage(date="2000-01-01", log_path=str(log)) == {}


def test_missing_file_returns_empty(tmp_path):
    assert daily_provider_usage(log_path=str(tmp_path / "nope.log")) == {}
