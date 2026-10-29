"""Barrera de red para pruebas: cualquier conexión fuera de loopback falla.

Impide que una prueba del agente o del adaptador haga una llamada real (facturable) a Anthropic,
a Meta o a Supabase, aunque exista una clave en el entorno.
"""

import socket

import pytest

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _blocked(*args, **kwargs):
    raise RuntimeError("red bloqueada en pruebas: conexión externa no permitida")


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    original_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in _LOOPBACK:
            _blocked()
        return original_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield
