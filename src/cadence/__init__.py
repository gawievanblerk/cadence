"""Cadence - a provider-agnostic conversational treatment engine.

The package is deliberately layered so that the three things the business owns
(treatment content, treatment rules, client data) never touch the thing it
rents (a language model).

    cadence.content        treatment program loaded from versioned YAML
    cadence.rules          deterministic evaluation of clinical rules
    cadence.memory         structured / episodic / semantic client memory
    cadence.rag            retrieval over the clinical knowledge base
    cadence.llm            the only place a vendor API is ever called
    cadence.orchestration  the state machine that binds the above together
    cadence.api            transport
"""

__version__ = "0.1.0"
