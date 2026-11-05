"""Service configuration from environment variables (documented in .env.example)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from shipex.agent.guardrails import GuardrailPolicy
from shipex.agent.loop import AgentBudget
from shipex.routing.rules import RoutingPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kafka_bootstrap: str = "localhost:19092"
    topic_partitions: int = 3
    database_url: str = "postgresql://shipex:shipex@localhost:5432/shipex"
    carrier_gateway_url: str = "http://localhost:8081"
    metrics_port: int = 9100
    consumer_max_attempts: int = 3

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_s: float = 60.0
    agent_model: str = "qwen/qwen3-30b-a3b"

    agent_max_steps: int = 8
    agent_max_cost_usd: float = 0.05
    agent_max_wall_s: float = 90.0
    agent_max_invalid_calls: int = 3

    high_value_gbp: float = 10_000.0
    auto_claim_limit_gbp: float = 500.0
    max_playbook_delay_hours: float = 24.0
    max_rebook_cost_delta_gbp: float = 150.0
    max_agent_claim_gbp: float = 2_000.0

    def routing_policy(self) -> RoutingPolicy:
        return RoutingPolicy(
            high_value_gbp=self.high_value_gbp,
            auto_claim_limit_gbp=self.auto_claim_limit_gbp,
            max_playbook_delay_hours=self.max_playbook_delay_hours,
        )

    def guardrail_policy(self) -> GuardrailPolicy:
        return GuardrailPolicy(
            max_rebook_cost_delta_gbp=self.max_rebook_cost_delta_gbp,
            max_agent_claim_gbp=self.max_agent_claim_gbp,
        )

    def agent_budget(self) -> AgentBudget:
        return AgentBudget(
            max_steps=self.agent_max_steps,
            max_cost_usd=self.agent_max_cost_usd,
            max_wall_s=self.agent_max_wall_s,
            max_invalid_calls=self.agent_max_invalid_calls,
        )
