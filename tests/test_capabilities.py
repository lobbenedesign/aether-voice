"""Verifies the plugin doesn't claim capabilities Moshi's wire protocol can't back up.

This is the test that would have caught aether-voice v1's problem: a
RealtimeCapabilities flag or a README line asserting something the code
underneath doesn't actually do. Every False here is False because the
`/api/chat` protocol (moshi/moshi/client.py, moshi_mlx/local_web.py) has no
message type for it — not because it was left unimplemented by choice.
"""

from livekit.plugins import moshi


def test_capabilities_match_the_wire_protocol():
    model = moshi.RealtimeModel()
    caps = model.capabilities

    # Real: the protocol streams Opus audio both directions.
    assert caps.audio_output is True

    # Honest: none of these have a corresponding message in the protocol.
    assert caps.message_truncation is False
    assert caps.turn_detection is False
    assert caps.user_transcription is False
    assert caps.auto_tool_reply_generation is False
    assert caps.manual_function_calls is False
    assert caps.can_disable_turn_detection is False
    assert caps.mutable_chat_context is False
    assert caps.mutable_instructions is False
    assert caps.mutable_tools is False
    assert caps.per_response_tool_choice is False
    assert caps.supports_say is False


def test_model_identity():
    model = moshi.RealtimeModel()
    assert model.provider == "kyutai"
    assert model.model == "moshi"
