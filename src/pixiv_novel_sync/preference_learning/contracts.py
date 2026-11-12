"""Private, server-created bindings for explicit preference learning."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PreferenceCallBinding:
    run_id: str
    owner_token: str
    configuration_fingerprint: str
