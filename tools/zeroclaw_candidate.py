"""Render the GitOps pointer to an exact CI-validated Noetrail commit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def render(revision: str, destination: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("A full lowercase Git commit SHA is required")
    destination.mkdir(parents=True, exist_ok=True)
    documents = {
        "kustomization.yaml": {
            "apiVersion": "kustomize.config.k8s.io/v1beta1",
            "kind": "Kustomization",
            "resources": ["revision.yaml"],
        },
        "revision.yaml": {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "noetrail-candidate", "namespace": "zeroclaw"},
            "data": {"revision": revision},
        },
    }
    for name, document in documents.items():
        (destination / name).write_text(json.dumps(document, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    render(args.revision, args.destination)


if __name__ == "__main__":
    main()
