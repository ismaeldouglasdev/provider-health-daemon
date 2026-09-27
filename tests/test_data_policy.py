from data_policy import (
    DataPolicy,
    DataSensitivity,
    allowed,
    classify_model_policy,
    classify_request,
    classify_text,
)


def test_plain_text_is_public():
    assert classify_text("Explain HTTP caching") == DataSensitivity.PUBLIC


def test_email_in_code_is_not_sensitive():
    assert classify_text("Contact maria@example.com") == DataSensitivity.PUBLIC


def test_secret_is_sensitive():
    assert classify_text("api_key=sk-test-123456789012345") == DataSensitivity.SENSITIVE


def test_repo_hint_is_internal():
    assert classify_text("Fix the private repository deployment") == DataSensitivity.INTERNAL


def test_message_parts_are_classified():
    body = {"messages": [{"role": "user", "content": "token: abcdefghijkl"}]}
    assert classify_request(body) == DataSensitivity.SENSITIVE


def test_nvidia_and_auto_free_are_training_possible():
    assert classify_model_policy("nvidia/glm-5.2") == DataPolicy.TRAINING_POSSIBLE
    assert classify_model_policy("kilo-auto/free") == DataPolicy.TRAINING_POSSIBLE


def test_local_models_are_local():
    assert classify_model_policy("ollama/qwen3") == DataPolicy.LOCAL


def test_known_provider_default_is_not_training_possible():
    assert classify_model_policy("openai/gpt-5.4") == DataPolicy.KNOWN_NO_TRAINING_DEFAULT


def test_unknown_is_allowed_for_sensitive_by_default():
    assert classify_model_policy("some-new-provider/model") == DataPolicy.UNKNOWN
    assert allowed(DataPolicy.UNKNOWN, DataSensitivity.SENSITIVE)


def test_training_possible_blocked_for_internal_and_sensitive():
    assert not allowed(DataPolicy.TRAINING_POSSIBLE, DataSensitivity.INTERNAL)
    assert not allowed(DataPolicy.TRAINING_POSSIBLE, DataSensitivity.SENSITIVE)


def test_public_allows_unknown():
    assert allowed(DataPolicy.UNKNOWN, DataSensitivity.PUBLIC)
