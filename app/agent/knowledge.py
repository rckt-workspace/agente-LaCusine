"""Conocimiento comercial versionado: prompt del sistema y políticas. Son datos fuente, no instrucciones."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
PROMPT_PATH = AGENT_DIR / "prompts" / "commercial_system.md"
POLICIES_PATH = AGENT_DIR / "policies" / "lacusine_policies.json"


@dataclass(frozen=True)
class Policies:
    version: str
    approved: tuple[dict, ...]
    pending_validation: tuple[dict, ...]
    escalation: tuple[dict, ...]


class KnowledgeService:
    def __init__(self, prompt_path: Path = PROMPT_PATH, policies_path: Path = POLICIES_PATH) -> None:
        self._system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        raw = json.loads(policies_path.read_text(encoding="utf-8"))
        self.policies = Policies(
            version=raw["version"],
            approved=tuple(raw["approved"]),
            pending_validation=tuple(raw["pending_validation"]),
            escalation=tuple(raw["escalation"]),
        )

    def system_prompt(self) -> str:
        """Prompt del sistema con las políticas incrustadas como datos de referencia."""
        policies = {
            "aprobadas": [p["statement"] for p in self.policies.approved],
            "pendientes_de_validacion_legal_no_afirmar": [p["statement"] for p in self.policies.pending_validation],
            "escalamiento_obligatorio": [p["statement"] for p in self.policies.escalation],
        }
        return (
            f"{self._system_prompt}\n\n"
            "# Políticas de referencia (datos, no instrucciones)\n\n"
            f"```json\n{json.dumps(policies, ensure_ascii=False, indent=2)}\n```"
        )
