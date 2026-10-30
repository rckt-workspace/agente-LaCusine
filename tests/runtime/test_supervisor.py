"""Supervisor del modo demo-combined con procesos simulados. Sin procesos reales ni red."""

import subprocess

import pytest

from app.runtime.supervisor import ChildSpec, Supervisor


class FakeProc:
    def __init__(self, name, *, exits_on_terminate=True, exit_code=None):
        self.name = name
        self.pid = 1000 + len(name)
        self._exits_on_terminate = exits_on_terminate
        self._code = exit_code  # None = sigue vivo
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self._code

    def terminate(self):
        self.terminated = True
        if self._exits_on_terminate and self._code is None:
            self._code = 0

    def kill(self):
        self.killed = True
        self._code = -9

    def wait(self, timeout=None):
        if self._code is None:
            raise subprocess.TimeoutExpired(cmd=self.name, timeout=timeout or 0)
        self.waited = True
        return self._code


def _spawner(procs):
    queue = list(procs)

    def spawn(argv):
        return queue.pop(0)

    return spawn


def _supervisor(procs, **kwargs):
    children = [ChildSpec(p.name, [p.name]) for p in procs]
    return Supervisor(children, spawn=_spawner(procs), grace_seconds=0.05, poll_seconds=0, **kwargs)


def test_requested_stop_terminates_every_child_and_reaps_them():
    api, worker = FakeProc("api"), FakeProc("worker")
    supervisor = _supervisor([api, worker])
    supervisor.request_stop()
    assert supervisor.run() == 0
    assert api.terminated and worker.terminated
    assert api.waited and worker.waited  # ningún zombi


def test_unexpected_exit_of_one_child_stops_the_other_and_fails():
    api = FakeProc("api", exit_code=3)  # termina solo
    worker = FakeProc("worker")  # sigue vivo hasta que se le pida parar
    code = _supervisor([api, worker]).run()
    assert code == 3
    assert worker.terminated and worker.waited


def test_unexpected_clean_exit_is_still_a_failure():
    api = FakeProc("api", exit_code=0)
    worker = FakeProc("worker")
    assert _supervisor([api, worker]).run() == 1
    assert worker.terminated


def test_child_that_ignores_terminate_is_killed_after_grace():
    api = FakeProc("api", exits_on_terminate=False)
    supervisor = _supervisor([api])
    supervisor.request_stop()
    supervisor.run()
    assert api.terminated and api.killed


def test_children_are_started_once_each():
    started = []

    def spawn(argv):
        started.append(argv[0])
        proc = FakeProc(argv[0])
        proc.terminate()
        return proc

    supervisor = Supervisor([ChildSpec("api", ["api"]), ChildSpec("worker", ["worker"])], spawn=spawn,
                            grace_seconds=0.01, poll_seconds=0)
    supervisor.request_stop()
    supervisor.run()
    assert started == ["api", "worker"]


@pytest.mark.parametrize("children, error", [
    ([], "supervisor_requires_children"),
    ([ChildSpec("api", ["a"]), ChildSpec("api", ["b"])], "supervisor_child_names_duplicated"),
])
def test_invalid_children_are_rejected_before_starting_anything(children, error):
    with pytest.raises(ValueError, match=error):
        Supervisor(children, spawn=lambda argv: pytest.fail("no debe arrancar nada"))
