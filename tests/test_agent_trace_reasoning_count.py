from types import SimpleNamespace

from app.services.agent_trace_statistics import reasoning_char_count


def test_saved_count_preserves_zero_and_is_independent_of_reasoning_tokens():
    for count in (0, 4231):
        sample = SimpleNamespace(payload={"competition_report": {"results": {"A2": {"known_characters": count}}},
                                          "token_usage": {"reasoning_tokens": None}}, result={})
        assert reasoning_char_count(sample) == count


def test_older_messages_count_unicode_and_shared_reasoning_once_per_message():
    sample = SimpleNamespace(payload={}, result={"source_record": {"messages": [
        {"role": "assistant", "reasoning_content": "😀\n a", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "content": "return"},
        {"role": "assistant", "reasoning_content": "😀\n a"},
    ]}})
    assert reasoning_char_count(sample) == 8


def test_missing_original_is_unknown_and_saved_empty_messages_count_is_zero():
    assert reasoning_char_count(SimpleNamespace(payload={}, result={})) is None
    assert reasoning_char_count(SimpleNamespace(payload={}, result={"source_record": {"messages": []}})) == 0
