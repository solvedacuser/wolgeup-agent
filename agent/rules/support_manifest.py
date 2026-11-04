from agent.rules.registry import WAGE_RULE_IDS


def support_manifest(registry):
    cells = []
    for profile in ("H-STD", "M-STD"):
        for rule_id in (*WAGE_RULE_IDS, "R-07"):
            rules = [r for r in registry.rules if r.profile == profile and r.rule_id == rule_id]
            cells.append(
                {
                    "cell_id": f"{profile}/{rule_id}",
                    "required_for_release": True,
                    "verification_status": "pending",
                    "rule_versions": [r.version for r in rules],
                }
            )
    return {
        "manifest_version": registry.manifest_version,
        "cells": cells,
        "release_ready": False,
        "reason": "Independent legal-rule and complete-profile verification pending",
    }
