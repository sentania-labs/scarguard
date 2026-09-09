from routes.admin import _buffer_text


def test_buffer_text_decodes_coupled_and_legacy_entries() -> None:
    coupled = '{"v":1,"text":"ready","identity":"container:timestamp:ready"}'

    assert _buffer_text(coupled) == "ready"
    assert _buffer_text("legacy line") == "legacy line"
    assert _buffer_text('{"event":"application-json"}') == '{"event":"application-json"}'
