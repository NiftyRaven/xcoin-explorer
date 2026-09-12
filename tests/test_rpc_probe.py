from explorer.rpc import PORT_LABEL, PROBE_PORTS


def test_mainnet_port_is_first():
    assert PROBE_PORTS[0] == 38442
    assert PORT_LABEL[38442] == "mainnet"
