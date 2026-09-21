import json
from types import SimpleNamespace

import pytest

from app.agents.llm_client import MistralLLMClient


def test_to_mistral_tools_translates_anthropic_schema():
    tools = [{"name": "search_products", "description": "Recherche", "input_schema": {"type": "object", "properties": {}}}]
    converted = MistralLLMClient._to_mistral_tools(tools)
    assert converted == [
        {
            "type": "function",
            "function": {"name": "search_products", "description": "Recherche", "parameters": {"type": "object", "properties": {}}},
        }
    ]


def test_to_mistral_messages_plain_text():
    messages = [{"role": "user", "content": "Bonjour"}]
    converted = MistralLLMClient._to_mistral_messages("Tu es Bob", messages)
    assert converted[0] == {"role": "system", "content": "Tu es Bob"}
    assert converted[1] == {"role": "user", "content": "Bonjour"}


def test_to_mistral_messages_assistant_tool_use_becomes_tool_calls():
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "search_products", "input": {"query": "Samsung"}}],
        }
    ]
    converted = MistralLLMClient._to_mistral_messages("sys", messages)
    assistant_msg = converted[1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"][0]["id"] == "toolu_1"
    assert assistant_msg["tool_calls"][0]["type"] == "function"
    assert json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"]) == {"query": "Samsung"}


def test_to_mistral_messages_tool_result_becomes_tool_role():
    messages = [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"stock": 5}'}]}
    ]
    converted = MistralLLMClient._to_mistral_messages("sys", messages)
    assert converted[1] == {"role": "tool", "tool_call_id": "toolu_1", "content": '{"stock": 5}'}


def _fake_client_returning(response):
    class FakeChat:
        async def complete_async(self, **kwargs):
            return response

    return SimpleNamespace(chat=FakeChat())


@pytest.mark.asyncio
async def test_create_message_text_response_maps_to_generic_envelope():
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Bonjour !", tool_calls=None))]
    )
    client = MistralLLMClient(api_key="x", model="mistral-large-latest")
    client._client = _fake_client_returning(response)

    result = await client.create_message(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert result == {"content": [{"type": "text", "text": "Bonjour !"}], "stop_reason": "end_turn"}


@pytest.mark.asyncio
async def test_create_message_tool_call_with_string_arguments():
    tool_call = SimpleNamespace(
        id="toolu_1", function=SimpleNamespace(name="search_products", arguments='{"query": "Samsung"}')
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[tool_call]))]
    )
    client = MistralLLMClient(api_key="x", model="mistral-large-latest")
    client._client = _fake_client_returning(response)

    result = await client.create_message(system="sys", messages=[{"role": "user", "content": "Samsung ?"}], tools=[])
    assert result["stop_reason"] == "tool_use"
    assert result["content"][0]["name"] == "search_products"
    assert result["content"][0]["input"] == {"query": "Samsung"}


@pytest.mark.asyncio
async def test_create_message_tool_call_with_dict_arguments():
    """Le SDK Mistral peut renvoyer les arguments déjà sous forme de dict plutôt qu'une chaîne JSON."""
    tool_call = SimpleNamespace(id="toolu_2", function=SimpleNamespace(name="check_stock", arguments={"product_id": "abc"}))
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[tool_call]))]
    )
    client = MistralLLMClient(api_key="x", model="mistral-large-latest")
    client._client = _fake_client_returning(response)

    result = await client.create_message(system="sys", messages=[{"role": "user", "content": "stock ?"}], tools=[])
    assert result["content"][0]["input"] == {"product_id": "abc"}
