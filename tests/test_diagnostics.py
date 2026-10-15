"""Диагностика сохраняет состав запроса, но не содержимое переписки."""

from types import SimpleNamespace

from karachur.gemini.diagnostics import describe_contents


def test_request_metadata_omits_private_text():
    private = "PRIVATE CHAT TEXT"
    result = describe_contents([
        {"role": "user", "parts": [{"text": private}]},
        SimpleNamespace(role="model", parts=[SimpleNamespace(
            text=None, file_data=SimpleNamespace(
                mime_type="video/mp4", file_uri="files/test"))]),
    ])
    assert private not in repr(result)
    assert result["text_chars"] == len(private)
    assert result["roles"] == {"user": 1, "model": 1}
    assert result["mimes"] == {"video/mp4": 1}
    assert result["media"] == [{"content": 1, "part": 0, "role": "model",
                                "mime": "video/mp4", "uri": "files/test"}]
