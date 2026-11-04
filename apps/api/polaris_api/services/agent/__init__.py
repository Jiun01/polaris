"""Agent tool-surface runtime (ADR-063).

A single tool-calling LLM agent per tenant, gated by
``tenants.settings.agent_config`` (default off → the classic
classify→rag→generate pipeline is unchanged). Hexagonal per ADR-007: the
tools depend on the ``Ports``/adapters in ``services/agent/ports.py``
(inherited from the now-retired Phase 5 flow runtime).

Entry point: ``pipeline_integration.try_run_agent`` — called by the inbound
pipeline before the classic branch when the tenant has the agent enabled.
"""
