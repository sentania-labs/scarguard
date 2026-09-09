from routes.admin import _buffer_text


def test_buffer_text_decodes_coupled_and_legacy_entries() -> None:
    coupled = (
        '{"__scarguard_log_buffer__":"scarguard-log-buffer-v1",'
        '"text":"ready","identity":"container:timestamp:ready"}'
    )
    legacy_lookalike = '{"v":1,"text":"app text","identity":"app identity"}'

    assert _buffer_text(coupled) == "ready"
    assert _buffer_text(legacy_lookalike) == legacy_lookalike
    assert _buffer_text("legacy line") == "legacy line"
    assert _buffer_text('{"event":"application-json"}') == '{"event":"application-json"}'
