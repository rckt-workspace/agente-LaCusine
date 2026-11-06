"""Identificación, parada verificada y comprobación de workers de La Cuisine. Solo toca procesos de ESTE repo.

Un worker en Windows son dos procesos: el lanzador del venv (con la ruta del repo en su línea de comando) y el
intérprete base hijo (cuya línea de comando NO contiene la ruta del repo). Por eso se identifica un árbol completo
a partir del lanzador y se termina con `taskkill /T`, que también mata a los hijos.

Comandos:
  list   --repo R                      muestra los árboles de workers de este repo
  stop-wait --repo R [--timeout S]     termina los árboles de este repo y ESPERA a que no quede ningún proceso
  check  --repo R --expect N           sale 0 solo si hay exactamente N árboles de workers de este repo
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable

WORKER_PATTERN = re.compile(r"app\.worker\.main")
_PS_SAMPLE = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
    "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
)


def _norm(text: str) -> str:
    return (text or "").replace("/", "\\").lower()


def find_worker_trees(processes: list[dict], repo: str) -> list[dict]:
    """Árboles de workers que pertenecen a `repo`. Función pura."""
    repo_norm = _norm(repo)
    by_pid = {int(p["ProcessId"]): p for p in processes}

    def cmd(pid: int) -> str:
        return by_pid[pid].get("CommandLine") or ""

    owned = {
        pid for pid in by_pid
        if WORKER_PATTERN.search(cmd(pid)) and repo_norm and repo_norm in _norm(cmd(pid))
    }
    # Hijos con worker que cuelgan de un proceso propio (intérprete base sin ruta del repo).
    grew = True
    while grew:
        grew = False
        for pid in by_pid:
            if pid in owned or not WORKER_PATTERN.search(cmd(pid)):
                continue
            if int(by_pid[pid].get("ParentProcessId") or 0) in owned:
                owned.add(pid)
                grew = True

    trees = []
    for root in sorted(owned):
        if int(by_pid[root].get("ParentProcessId") or 0) in owned:
            continue  # no es raíz: ya está dentro de un árbol
        members = {root}
        frontier = [root]
        while frontier:
            current = frontier.pop()
            for pid in owned:
                if int(by_pid[pid].get("ParentProcessId") or 0) == current and pid not in members:
                    members.add(pid)
                    frontier.append(pid)
        trees.append({"root": root, "pids": sorted(members)})
    return trees


def alive_pids(processes: list[dict], pids: set[int]) -> set[int]:
    present = {int(p["ProcessId"]) for p in processes}
    return pids & present


def wait_until_gone(pids: set[int], sample: Callable[[], list[dict]], timeout_seconds: float,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> bool:
    """True cuando ninguno de `pids` sigue vivo. False si el tiempo se agota (un worker puede tardar en salir)."""
    deadline = clock() + timeout_seconds
    while True:
        if not alive_pids(sample(), pids):
            return True
        if clock() >= deadline:
            return False
        sleep(1.0)


def sample_processes() -> list[dict]:
    """Procesos Python del sistema. Solo lectura."""
    result = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_SAMPLE],
                            capture_output=True, text=True, check=True)
    data = json.loads(result.stdout or "[]")
    return data if isinstance(data, list) else [data]


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)


def stop_and_wait(repo: str, timeout_seconds: float, sample=sample_processes, kill=_kill_tree,
                  sleep=time.sleep, clock=time.monotonic) -> tuple[bool, list[int]]:
    trees = find_worker_trees(sample(), repo)
    pids = {pid for tree in trees for pid in tree["pids"]}
    for tree in trees:
        kill(tree["root"])
    gone = wait_until_gone(pids, sample, timeout_seconds, sleep=sleep, clock=clock)
    remaining = sorted(alive_pids(sample(), pids)) if not gone else []
    return gone, remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--repo", required=True)
    stop_cmd = sub.add_parser("stop-wait")
    stop_cmd.add_argument("--repo", required=True)
    stop_cmd.add_argument("--timeout", type=float, default=45.0)
    check_cmd = sub.add_parser("check")
    check_cmd.add_argument("--repo", required=True)
    check_cmd.add_argument("--expect", type=int, required=True)
    args = parser.parse_args(argv)

    if args.command == "list":
        trees = find_worker_trees(sample_processes(), args.repo)
        print(json.dumps({"count": len(trees), "trees": trees}))
        return 0
    if args.command == "stop-wait":
        trees = find_worker_trees(sample_processes(), args.repo)
        print(f"workers_encontrados={len(trees)}")
        gone, remaining = stop_and_wait(args.repo, args.timeout)
        if not gone:
            print(f"NO_TERMINARON pids={remaining}")
            return 1
        print("workers_detenidos=ok")
        return 0
    trees = find_worker_trees(sample_processes(), args.repo)
    print(f"workers_vivos={len(trees)} esperado={args.expect}")
    return 0 if len(trees) == args.expect else 1


if __name__ == "__main__":
    sys.exit(main())
