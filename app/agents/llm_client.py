"""
Abstraction du client LLM. Permet de tester l'orchestrateur (boucle d'outils, garde-fous)
sans dépendre du réseau réel ni d'une clé API — un FakeLLMClient suffit en test.
"""
from abc import ABC, abstractmethod
from typing import Any


class LLMClient(ABC):
    @abstractmethod
    async def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 1024
    ) -> dict[str, Any]:
        """
        Retourne un dict au format de la réponse Anthropic Messages API :
        {"content": [{"type": "text"|"tool_use", ...}], "stop_reason": "end_turn"|"tool_use"|...}
        """
        raise NotImplementedError


class MistralLLMClient(LLMClient):
    """
    Implémentation Mistral de la même interface LLMClient. L'orchestrateur, les outils
    et les garde-fous restent identiques : seule cette classe traduit le format interne
    (inspiré d'Anthropic) vers l'API Mistral, et traduit sa réponse en retour.
    """

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            from mistralai.client.sdk import Mistral  # import différé

            self._client = Mistral(api_key=self.api_key)
        return self._client

    @staticmethod
    def _to_mistral_tools(tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
            }
            for t in tools
        ]

    @staticmethod
    def _to_mistral_messages(system: str, messages: list[dict]) -> list[dict]:
        import json

        mistral_messages: list[dict] = [{"role": "system", "content": system}]

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                mistral_messages.append({"role": role, "content": content})
                continue

            if role == "assistant":
                text_parts = [b["text"] for b in content if b.get("type") == "text"]
                tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]
                assistant_msg: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
                if tool_use_blocks:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": b["id"],
                            "type": "function",
                            "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)},
                        }
                        for b in tool_use_blocks
                    ]
                mistral_messages.append(assistant_msg)
            else:  # role == "user" portant des résultats d'outils
                for b in content:
                    if b.get("type") == "tool_result":
                        mistral_messages.append(
                            {"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]}
                        )

        return mistral_messages

    async def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 1024
    ) -> dict[str, Any]:
        import json

        client = self._get_client()
        response = await client.chat.complete_async(
            model=self.model,
            messages=self._to_mistral_messages(system, messages),
            tools=self._to_mistral_tools(tools) if tools else None,
            max_tokens=max_tokens,
        )

        choice = response.choices[0]
        message = choice.message

        if message.tool_calls:
            content_blocks = []
            for tc in message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, str):
                    args = json.loads(args) if args else {}
                content_blocks.append({"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args})
            return {"content": content_blocks, "stop_reason": "tool_use"}

        text = message.content
        if isinstance(text, list):  # chunks structurés plutôt qu'une simple chaîne
            text = "".join(chunk.text for chunk in text if getattr(chunk, "type", None) == "text")
        return {"content": [{"type": "text", "text": text or ""}], "stop_reason": "end_turn"}


class AnthropicLLMClient(LLMClient):
    """Câblage réel — utilisé en production quand ANTHROPIC_API_KEY est configurée."""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # import différé : pas de dépendance dure si le LLM n'est jamais utilisé

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], max_tokens: int = 1024
    ) -> dict[str, Any]:
        client = self._get_client()
        response = await client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
        return response.model_dump()
