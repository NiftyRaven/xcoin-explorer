from explorer.decode import decode_address, encode_address, hash160, is_p2pkh, parse_vout_script


def test_p2pkh_roundtrip_main():
    payload = hash160(b"xcoin-test-key")
    addr = encode_address(payload, 76)
    ver, got = decode_address(addr)
    assert ver == 76
    assert got == payload
    assert addr.startswith("X")


def test_parse_p2pkh_script():
    payload = bytes.fromhex("11" * 20)
    script = bytes([0x76, 0xA9, 0x14]) + payload + bytes([0x88, 0xAC])
    assert is_p2pkh(script)
    parsed = parse_vout_script(script.hex(), "main")
    assert parsed["script_type"] == "pubkeyhash"
    assert parsed["address"]
    assert parsed["address"].startswith("X")
